import importlib
import logging
import os
import shutil
import socket
import sys
import threading
import time
from queue import Full, Queue
from concurrent import futures
from pathlib import Path

import grpc
import numpy as np

import algorithm.config_helper as config_helper
from algorithm.agent import Agent

from . import constants as C
from .proto import (evolver_pb2, evolver_pb2_grpc, learner_pb2,
                    learner_pb2_grpc, replay_pb2, replay_pb2_grpc)
from .proto.ndarray_pb2 import Empty
from .proto.numproto import ndarray_to_proto, proto_to_ndarray
from .proto.pingpong_pb2 import Ping, Pong
from .sac_ds_base import SAC_DS_Base
from .utils import PeerSet, rpc_error_inspector


class SampledDataBuffer:
    def __init__(self, get_sampled_data):
        self._get_sampled_data = get_sampled_data

        self._data_feeded = False
        self._closed = False
        self._buffer = Queue(maxsize=C.SAMPLED_DATA_BUFFER_MAXSIZE)
        self.logger = logging.getLogger('ds.learner.sampled_data_buffer')

        t = threading.Thread(target=self.run, daemon=True)
        t.start()

    def run(self):
        while not self._closed:
            sampled = self._get_sampled_data()

            if sampled is not None:
                self._buffer.put(sampled)
                self._data_feeded = True
            else:
                self.logger.warning('No data sampled')
                if self._data_feeded:
                    self.logger.warning('Replay is offline')
                    self.close()
                    break

                time.sleep(C.RECONNECTION_TIME)

    def get_data(self):
        _t = time.time()
        data = self._buffer.get()
        if time.time() - _t > 0.01:
            self.logger.warning(f'Getting data spent {time.time() - _t}s')
        return data

    def close(self):
        self._closed = True


class UpdateDataBuffer:
    def __init__(self, update_td_error, update_transitions):
        self._update_td_error = update_td_error
        self._update_transitions = update_transitions

        self._closed = False
        self._buffer = Queue(maxsize=C.UPDATE_DATA_BUFFER_MAXSIZE)
        self.logger = logging.getLogger('ds.learner.update_data_buffer')

        ts = [threading.Thread(target=self.run, daemon=True) for _ in range(C.UPDATE_DATA_BUFFER_THREADS)]
        for t in ts:
            t.start()

    def run(self):
        while not self._closed:
            is_td_error, *data = self._buffer.get()
            if is_td_error:
                pointers, td_error = data
                self._update_td_error(pointers, td_error)
            else:
                pointers, key, data = data
                self._update_transitions(pointers, key, data)

    def add_data(self, is_td_error, *data):
        try:
            self._buffer.put_nowait((is_td_error, *data))
        except Full:
            self.logger.warning('Buffer is full, ignored')

    def close(self):
        self._closed = True


class Learner:
    _agent_class = Agent

    def __init__(self, root_dir, config_dir, args):
        self.root_dir = root_dir
        self.cmd_args = args

        self.logger = logging.getLogger('ds.learner')

        constant_config, config_abs_dir = self._init_constant_config(root_dir, config_dir, args)
        self.net_config = constant_config['net_config']

        self._stub = StubController(self.net_config['evolver_host'],
                                    self.net_config['evolver_port'],
                                    self.net_config['learner_host'],
                                    self.net_config['learner_port'],
                                    self.net_config['replay_host'],
                                    self.net_config['replay_port'])

        self._is_training = False
        self._closed = False
        self._sac_lock = threading.Lock()
        self._sac_bak_lock = threading.Lock()
        self.registered = False

        try:
            self._init_env(constant_config, config_abs_dir)
            self._run()

        except KeyboardInterrupt:
            self.logger.warning('KeyboardInterrupt in _run')

        finally:
            self.close()

    def _init_constant_config(self, root_dir, config_dir, args):
        config_abs_dir = Path(root_dir).joinpath(config_dir)
        config_abs_path = config_abs_dir.joinpath('config_ds.yaml')
        config = config_helper.initialize_config_from_yaml(f'{Path(__file__).resolve().parent}/default_config.yaml',
                                                           config_abs_path,
                                                           args.config)

        # Initialize config from command line arguments
        self.standalone = args.standalone
        self.last_ckpt = args.ckpt
        self.render = args.render
        self.run_in_editor = args.editor

        if args.evolver_host is not None:
            config['net_config']['evolver_host'] = args.evolver_host
        if args.evolver_port is not None:
            config['net_config']['evolver_port'] = args.evolver_port
        if args.learner_host is not None:
            config['net_config']['learner_host'] = args.learner_host
        if args.learner_port is not None:
            config['net_config']['learner_port'] = args.learner_port
        if args.replay_host is not None:
            config['net_config']['replay_host'] = args.replay_host
        if args.replay_port is not None:
            config['net_config']['replay_port'] = args.replay_port

        if args.in_k8s:
            hostname = socket.gethostname()
            host_id = hostname.split('-')[-1]
            learner_host = config['net_config']['learner_host']
            replay_host = config['net_config']['replay_host']
            config['net_config']['learner_host'] = f'{learner_host}-{host_id}.{learner_host}'
            config['net_config']['replay_host'] = f'{replay_host}-{host_id}.{replay_host}'

        return config, config_abs_dir

    def _init_env(self, config, config_abs_dir):
        if self.cmd_args.name is not None:
            config['base_config']['name'] = self.cmd_args.name
        if self.cmd_args.build_port is not None:
            config['base_config']['build_port'] = self.cmd_args.build_port
        if self.cmd_args.nn is not None:
            config['base_config']['nn'] = self.cmd_args.nn
        if self.cmd_args.agents is not None:
            config['base_config']['n_agents'] = self.cmd_args.agents

        self.config = config['base_config']
        sac_config = config['sac_config']
        self.reset_config = config['reset_config']

        # Get name from evolver registry
        evolver_register_response = None
        self.logger.info('Registering...')
        while evolver_register_response is None:
            if not self._stub.connected:
                time.sleep(C.RECONNECTION_TIME)
                continue
            evolver_register_response = self._stub.register_to_evolver()
            if evolver_register_response is None:
                time.sleep(C.RECONNECTION_TIME)

        _id, name = evolver_register_response
        self.id = _id
        self.config['name'] = name
        self.registered = True
        self.logger.info(f'Registered id: {_id}, name: {name}')

        model_abs_dir = Path(self.root_dir).joinpath('models',
                                                     self.config['scene'],
                                                     self.config['name'],
                                                     f'learner{_id}')
        self.model_abs_dir = model_abs_dir
        os.makedirs(model_abs_dir)

        if self.cmd_args.logger_in_file:
            config_helper.set_logger(Path(model_abs_dir).joinpath(f'learner.log'))

        config_helper.display_config(config, self.logger)

        if self.config['env_type'] == 'UNITY':
            from algorithm.env_wrapper.unity_wrapper import UnityWrapper

            if self.run_in_editor:
                self.env = UnityWrapper(base_port=5004)
            else:
                self.env = UnityWrapper(file_name=self.config['build_path'][sys.platform],
                                        base_port=self.config['build_port'],
                                        no_graphics=self.config['no_graphics'],
                                        scene=self.config['scene'],
                                        n_agents=self.config['n_agents'])

        elif self.config['env_type'] == 'GYM':
            from algorithm.env_wrapper.gym_wrapper import GymWrapper

            self.env = GymWrapper(env_name=self.config['build_path'],
                                  n_agents=self.config['n_agents'])
        else:
            raise RuntimeError(f'Undefined Environment Type: {self.config["env_type"]}')

        self.obs_dims, self.action_dim, is_discrete = self.env.init()

        self.logger.info(f'{self.config["build_path"]} initialized')

        # If model exists, load saved model, or copy a new one
        nn_model_abs_path = Path(model_abs_dir).joinpath('nn_models.py')
        if os.path.isfile(nn_model_abs_path):
            spec = importlib.util.spec_from_file_location('nn', str(nn_model_abs_path))
        else:
            nn_abs_path = Path(config_abs_dir).joinpath(f'{self.config["nn"]}.py')
            spec = importlib.util.spec_from_file_location('nn', str(nn_abs_path))
            shutil.copyfile(nn_abs_path, nn_model_abs_path)

        custom_nn_model = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(custom_nn_model)

        self.sac = SAC_DS_Base(obs_dims=self.obs_dims,
                               action_dim=self.action_dim,
                               is_discrete=is_discrete,
                               model_abs_dir=model_abs_dir,
                               model=custom_nn_model,
                               last_ckpt=self.last_ckpt,

                               **sac_config)

        self.sac_bak = SAC_DS_Base(train_mode=False,
                                   obs_dims=self.obs_dims,
                                   action_dim=self.action_dim,
                                   is_discrete=is_discrete,
                                   model_abs_dir=None,
                                   model=custom_nn_model,
                                   last_ckpt=self.last_ckpt,

                                   **sac_config)

        self._update_sac_bak()
        self.logger.info(f'sac_learner initialized')

    _unique_id = -1

    def _get_model_abs_dir(self):
        if self.registered:
            self._unique_id += 1
            return str(self.model_abs_dir), self._unique_id

    def _get_policy_variables(self):
        with self._sac_bak_lock:
            variables = self.sac_bak.get_policy_variables()

        return [v.numpy() for v in variables]

    def _get_nn_variables(self):
        with self._sac_lock:
            variables = self.sac.get_nn_variables()

        return [v.numpy() for v in variables]

    def _udpate_nn_variables(self, variables):
        with self._sac_lock:
            self.sac.update_nn_variables(variables)

        self._update_sac_bak()

        self.logger.info('Updated all nn variables')

    def _update_sac_bak(self):
        with self._sac_lock:
            variables = self.sac.get_all_variables()

        with self._sac_bak_lock:
            self.sac_bak.update_all_variables(variables)

    def _get_action(self, obs_list, rnn_state=None):
        if self.sac_bak.use_rnn:
            assert rnn_state is not None

        with self._sac_bak_lock:
            if self.sac_bak.use_rnn:
                action, next_rnn_state = self.sac_bak.choose_rnn_action(obs_list, rnn_state)
                next_rnn_state = next_rnn_state
                return action.numpy(), next_rnn_state.numpy()
            else:
                action = self.sac_bak.choose_action(obs_list)
                return action.numpy()

    def _get_td_error(self,
                      n_obses_list,
                      n_actions,
                      n_rewards,
                      next_obs_list,
                      n_dones,
                      n_mu_probs,
                      n_rnn_states=None):
        """
        n_obses_list: list([1, episode_len, obs_dim_i], ...)
        n_actions: [1, episode_len, action_dim]
        n_rewards: [1, episode_len]
        next_obs_list: list([1, obs_dim_i], ...)
        n_dones: [1, episode_len]
        n_rnn_states: [1, episode_len, rnn_state_dim]
        """
        with self._sac_bak_lock:
            td_error = self.sac_bak.get_episode_td_error(n_obses_list=n_obses_list,
                                                         n_actions=n_actions,
                                                         n_rewards=n_rewards,
                                                         next_obs_list=next_obs_list,
                                                         n_dones=n_dones,
                                                         n_mu_probs=n_mu_probs,
                                                         n_rnn_states=n_rnn_states if self.sac.use_rnn else None)
        return td_error

    def _policy_evaluation(self):
        try:
            use_rnn = self.sac_bak.use_rnn

            iteration = 0
            start_time = time.time()

            obs_list = self.env.reset(reset_config=self.reset_config)

            agents = [self._agent_class(i, use_rnn=use_rnn)
                      for i in range(self.config['n_agents'])]

            if use_rnn:
                initial_rnn_state = self.sac_bak.get_initial_rnn_state(len(agents))
                rnn_state = initial_rnn_state

            while not self._closed:
                # Not training, waiting...
                if not self._is_training:
                    time.sleep(C.EVALUATION_WAITING_TIME)
                    continue

                if self.config['reset_on_iteration']:
                    obs_list = self.env.reset(reset_config=self.reset_config)
                    for agent in agents:
                        agent.clear()

                    if use_rnn:
                        rnn_state = initial_rnn_state
                else:
                    for agent in agents:
                        agent.reset()

                action = np.zeros([len(agents), self.action_dim], dtype=np.float32)
                step = 0

                while False in [a.done for a in agents] and \
                        not self._closed and self._is_training:
                    with self._sac_bak_lock:
                        if use_rnn:
                            action, next_rnn_state = self.sac_bak.choose_rnn_action([o.astype(np.float32) for o in obs_list],
                                                                                    action,
                                                                                    rnn_state)
                            next_rnn_state = next_rnn_state.numpy()
                        else:
                            action = self.sac_bak.choose_action([o.astype(np.float32) for o in obs_list])

                    action = action.numpy()

                    next_obs_list, reward, local_done, max_reached = self.env.step(action)

                    if step == self.config['max_step_per_iter']:
                        local_done = [True] * len(agents)
                        max_reached = [True] * len(agents)

                    for i, agent in enumerate(agents):
                        agent.add_transition([o[i] for o in obs_list],
                                             action[i],
                                             reward[i],
                                             local_done[i],
                                             max_reached[i],
                                             [o[i] for o in next_obs_list],
                                             rnn_state[i] if use_rnn else None)

                    obs_list = next_obs_list
                    action[local_done] = np.zeros(self.action_dim)
                    if use_rnn:
                        rnn_state = next_rnn_state
                        rnn_state[local_done] = initial_rnn_state[local_done]

                    step += 1

                self._log_episode_summaries(iteration, agents)

                if not self.standalone:
                    self._stub.post_reward(np.mean([a.reward for a in agents]))

                self._log_episode_info(iteration, start_time, agents)

                iteration += 1

                if self.standalone:
                    time.sleep(C.EVALUATION_INTERVAL)

            self.logger.warning('Evaluation exits')
        except Exception as e:
            self.logger.error(e)

    def _log_episode_summaries(self, iteration, agents):
        # iteration has no effect, the real step is the `global_step` in sac_base
        rewards = np.array([a.reward for a in agents])
        with self._sac_lock:
            self.sac.write_constant_summaries([
                {'tag': 'reward/mean', 'simple_value': rewards.mean()},
                {'tag': 'reward/max', 'simple_value': rewards.max()},
                {'tag': 'reward/min', 'simple_value': rewards.min()}
            ], iteration)

    def _log_episode_info(self, iteration, start_time, agents):
        time_elapse = (time.time() - start_time) / 60
        rewards = [a.reward for a in agents]
        rewards = ", ".join([f"{i:6.1f}" for i in rewards])
        self.logger.info(f'{iteration}, {time_elapse:.2f}, R {rewards}')

    def _run_training_client(self):
        self._stub.clear_replay_buffer()
        sample_data_buffer = SampledDataBuffer(self._stub.get_sampled_data)
        update_data_buffer = UpdateDataBuffer(self._stub.update_td_error,
                                              self._stub.update_transitions)

        while self._stub.connected:
            (pointers,
             n_obses_list,
             n_actions,
             n_rewards,
             next_obs_list,
             n_dones,
             n_mu_probs,
             rnn_state,
             priority_is) = sample_data_buffer.get_data()

            self._is_training = True

            with self._sac_lock:
                step, td_error, update_data = self.sac.train(pointers=pointers,
                                                             n_obses_list=n_obses_list,
                                                             n_actions=n_actions,
                                                             n_rewards=n_rewards,
                                                             next_obs_list=next_obs_list,
                                                             n_dones=n_dones,
                                                             n_mu_probs=n_mu_probs,
                                                             priority_is=priority_is,
                                                             rnn_state=rnn_state)

            if np.isnan(np.min(td_error)):
                self.logger.error('NAN in td_error')
                self.sac.save_model()
                self.close()
                break

            if step % self.config['update_sac_bak_per_step'] == 0:
                self._update_sac_bak()

            update_data_buffer.add_data(True, pointers, td_error)
            for pointers, key, data in update_data:
                update_data_buffer.add_data(False, pointers, key, data)

        sample_data_buffer.close()
        update_data_buffer.close()
        self.logger.warning('Training exits')

    def _run(self):
        t_evaluation = threading.Thread(target=self._policy_evaluation, daemon=True)
        t_evaluation.start()

        servicer = LearnerService(self._get_model_abs_dir,
                                  self._get_action,
                                  self._get_policy_variables,
                                  self._get_nn_variables,
                                  self._udpate_nn_variables,
                                  self._get_td_error)
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=C.MAX_THREAD_WORKERS),
                                  options=[
            ('grpc.max_send_message_length', C.MAX_MESSAGE_LENGTH),
            ('grpc.max_receive_message_length', C.MAX_MESSAGE_LENGTH)
        ])
        learner_pb2_grpc.add_LearnerServiceServicer_to_server(servicer, self.server)
        self.server.add_insecure_port(f'[::]:{self.net_config["learner_port"]}')
        self.server.start()
        self.logger.info(f'Learner server is running on [{self.net_config["learner_port"]}]...')

        self._run_training_client()

    def close(self):
        self._is_training = False
        self._closed = True

        if hasattr(self, 'env'):
            self.env.close()
        if hasattr(self, 'server'):
            self.server.stop(None)

        self._stub.close()

        self.logger.warning('Closed')


class LearnerService(learner_pb2_grpc.LearnerServiceServicer):
    def __init__(self,
                 get_model_abs_dir,
                 get_action,
                 get_policy_variables,
                 get_nn_variables,
                 udpate_nn_variables,
                 get_td_error):
        self._get_model_abs_dir = get_model_abs_dir
        self._get_action = get_action
        self._get_policy_variables = get_policy_variables
        self._get_nn_variables = get_nn_variables
        self._udpate_nn_variables = udpate_nn_variables
        self._get_td_error = get_td_error

        self._peer_set = PeerSet(logging.getLogger('ds.learner.service'))

    def _record_peer(self, context):
        def _unregister_peer():
            self._peer_set.disconnect(context.peer())
        context.add_callback(_unregister_peer)
        self._peer_set.connect(context.peer())

    def Persistence(self, request_iterator, context):
        self._record_peer(context)
        for request in request_iterator:
            yield Pong(time=int(time.time() * 1000))

    def GetModelAbsDir(self, request, context):
        dir_id = self._get_model_abs_dir()
        if dir_id is not None:
            _dir, _id = dir_id
            return learner_pb2.ModelAbsDir(dir=_dir, unique_id=_id)
        else:
            return learner_pb2.ModelAbsDir(unique_id=-1)

    # From actor
    def GetAction(self, request: learner_pb2.GetActionRequest, context):
        obs_list = [proto_to_ndarray(obs) for obs in request.obs_list]
        rnn_state = proto_to_ndarray(request.rnn_state)

        if rnn_state is None:
            action = self._get_action(obs_list)
            next_rnn_state = None
        else:
            action, next_rnn_state = self._get_action(obs_list, rnn_state)

        return learner_pb2.Action(action=ndarray_to_proto(action),
                                  rnn_state=ndarray_to_proto(next_rnn_state))

    # From actor
    def GetPolicyVariables(self, request, context):
        variables = self._get_policy_variables()
        return learner_pb2.NNVariables(variables=[ndarray_to_proto(v) for v in variables])

    # From evolver
    def GetNNVariables(self, request, context):
        variables = self._get_nn_variables()
        return learner_pb2.NNVariables(variables=[ndarray_to_proto(v) for v in variables])

    # From evolver
    def UpdateNNVariables(self, request, context):
        variables = [proto_to_ndarray(v) for v in request.variables]
        self._udpate_nn_variables(variables)
        return Empty()

    # From replay
    def GetTDError(self, request: learner_pb2.GetTDErrorRequest, context):
        n_obses_list = [proto_to_ndarray(n_obses) for n_obses in request.n_obses_list]
        n_actions = proto_to_ndarray(request.n_actions)
        n_rewards = proto_to_ndarray(request.n_rewards)
        next_obs_list = [proto_to_ndarray(next_obs) for next_obs in request.next_obs_list]
        n_dones = proto_to_ndarray(request.n_dones)
        n_mu_probs = proto_to_ndarray(request.n_mu_probs)
        n_rnn_states = proto_to_ndarray(request.n_rnn_states)

        td_error = self._get_td_error(n_obses_list,
                                      n_actions,
                                      n_rewards,
                                      next_obs_list,
                                      n_dones,
                                      n_mu_probs,
                                      n_rnn_states)
        return learner_pb2.TDError(td_error=ndarray_to_proto(td_error))


class StubController:
    _closed = False

    def __init__(self, evolver_host, evolver_port,
                 learner_host, learner_port,
                 replay_host, replay_port):
        self.learner_host = learner_host
        self.learner_port = learner_port
        self.replay_host = replay_host
        self.replay_port = replay_port

        self._evolver_channel = grpc.insecure_channel(f'{evolver_host}:{evolver_port}', [
            ('grpc.max_reconnect_backoff_ms', C.MAX_RECONNECT_BACKOFF_MS),
            ('grpc.max_send_message_length', C.MAX_MESSAGE_LENGTH),
            ('grpc.max_receive_message_length', C.MAX_MESSAGE_LENGTH)
        ])
        self._evolver_stub = evolver_pb2_grpc.EvolverServiceStub(self._evolver_channel)
        self._evolver_connected = False

        self._replay_channel = grpc.insecure_channel(f'{replay_host}:{replay_port}', [
            ('grpc.max_reconnect_backoff_ms', C.MAX_RECONNECT_BACKOFF_MS),
            ('grpc.max_send_message_length', C.MAX_MESSAGE_LENGTH),
            ('grpc.max_receive_message_length', C.MAX_MESSAGE_LENGTH)
        ])
        self._replay_stub = replay_pb2_grpc.ReplayServiceStub(self._replay_channel)

        self._logger = logging.getLogger('ds.learner.stub')

        t_evolver = threading.Thread(target=self._start_evolver_persistence, daemon=True)
        t_evolver.start()

    @property
    def connected(self):
        return not self._closed and self._evolver_connected

    # To replay
    @rpc_error_inspector
    def get_sampled_data(self):
        response = self._replay_stub.Sample(Empty())
        if response and response.has_data:
            return (proto_to_ndarray(response.pointers),
                    [proto_to_ndarray(n_obses) for n_obses in response.n_obses_list],
                    proto_to_ndarray(response.n_actions),
                    proto_to_ndarray(response.n_rewards),
                    [proto_to_ndarray(next_obs) for next_obs in response.next_obs_list],
                    proto_to_ndarray(response.n_dones),
                    proto_to_ndarray(response.n_mu_probs),
                    proto_to_ndarray(response.rnn_state),
                    proto_to_ndarray(response.priority_is))

    # To replay
    @rpc_error_inspector
    def update_td_error(self, pointers, td_error):
        self._replay_stub.UpdateTDError(
            replay_pb2.UpdateTDErrorRequest(pointers=ndarray_to_proto(pointers),
                                            td_error=ndarray_to_proto(td_error)))

    # To replay
    @rpc_error_inspector
    def update_transitions(self, pointers, key, data):
        self._replay_stub.UpdateTransitions(
            replay_pb2.UpdateTransitionsRequest(pointers=ndarray_to_proto(pointers),
                                                key=key,
                                                data=ndarray_to_proto(data)))

    # To replay
    @rpc_error_inspector
    def clear_replay_buffer(self):
        self._replay_stub.Clear(Empty())

    @rpc_error_inspector
    def register_to_evolver(self):
        response = self._evolver_stub.RegisterLearner(
            evolver_pb2.RegisterLearnerRequest(learner_host=self.learner_host,
                                               learner_port=self.learner_port,
                                               replay_host=self.replay_host,
                                               replay_port=self.replay_port))

        if response:
            return response.id, response.name

    # To evolver
    @rpc_error_inspector
    def post_reward(self, reward):
        self._evolver_stub.PostReward(
            evolver_pb2.PostRewardToEvolverRequest(reward=float(reward)))

    def _start_evolver_persistence(self):
        def request_messages():
            while not self._closed:
                yield Ping(time=int(time.time() * 1000))
                time.sleep(C.PING_INTERVAL)
                if not self._evolver_connected:
                    break

        while not self._closed:
            try:
                reponse_iterator = self._evolver_stub.Persistence(request_messages())
                for response in reponse_iterator:
                    if not self._evolver_connected:
                        self._evolver_connected = True
                        self._logger.info('Evolver connected')
            except grpc.RpcError:
                if self._evolver_connected:
                    self._evolver_connected = False
                    self._logger.error('Evolver disconnected')
            finally:
                time.sleep(C.RECONNECTION_TIME)

    def close(self):
        self._evolver_channel.close()
        self._replay_channel.close()
        self._closed = True
