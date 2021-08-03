import tensorflow as tf
import numpy as np
import itertools
import functools
import time

from mpi4py import MPI

from RLWorkflow.common.mpi_adam_optimizer import MpiAdamOptimizer
from RLWorkflow.common.tf_util import get_session, save_variables, load_variables, initialize
import RLWorkflow.common.tf_util as U
from RLWorkflow.common.mpi_util import sync_from_root
from RLWorkflow.common.console_util import fmt_row
from RLWorkflow.offloading_ppo.ann_policy import ANNPolicy
from RLWorkflow import logger

from RLWorkflow.environment.offloading_env import OffloadingEnvironment
from RLWorkflow.environment.offloading_env import Resources

from RLWorkflow.common.dataset import Dataset
from RLWorkflow.common.misc_util import zipsame

def calculate_qoe(latency_batch, energy_batch, env):
    all_local_time, all_local_energy = env.get_all_locally_execute_time_batch()
    all_local_time = np.squeeze(all_local_time)
    all_local_energy = np.squeeze(all_local_energy)
    latency_batch = np.squeeze(latency_batch)
    energy_batch = np.squeeze(energy_batch)
    qoe_batch = []

    for latency, energy, single_all_local_latency, single_all_local_energy in zip(latency_batch, energy_batch, all_local_time, all_local_energy):
        qoe = env.lambda_t * ((latency - single_all_local_latency) / single_all_local_latency) + \
              env.lambda_e * ((energy - single_all_local_energy) / single_all_local_energy)

        qoe = -qoe
        qoe_batch.append(qoe)

    return qoe_batch

class ANNPPOModel(object):
    def __init__(self, obs_dim, action_dim, hidden_units, ent_coef, vf_coef, max_grad_norm):
        sess = get_session()
        # sequential state

        # sequential action
        obs = tf.placeholder(tf.float32, [None, None, obs_dim])
        action = tf.placeholder(tf.int32, [None, None])
        # sequential adv
        adv = tf.placeholder(tf.float32, [None, None])
        # sequential return
        ret = tf.placeholder(tf.float32, [None, None])

        # keep track of old actor(sequential descision)
        oldneglogpac = tf.placeholder(tf.float32, [None, None])
        oldvpred = tf.placeholder(tf.float32, [None, None])
        lr = tf.placeholder(tf.float32, [])

        # Cliprange
        cliprange = tf.placeholder(tf.float32, [])

        train_model = ANNPolicy("pi", obs, action, hidden_units=hidden_units, reuse=True, action_dim=action_dim)
        act_model = ANNPolicy("oldpi", obs, action, hidden_units=hidden_units, reuse=False, action_dim=action_dim)

        assign_old_eq_new = U.function([], [], updates=[tf.assign(oldv, newv)
                                                        for (oldv, newv) in
                                                        zipsame(act_model.get_variables(),
                                                                train_model.get_variables())])

        # Entropy is used to improve exploration by limiting the premature convergence to suboptimal policy.
        entropy = tf.reduce_mean(train_model.entropy())

        vpred = train_model.vf
        vf_losses1 = tf.square(vpred - ret)
        vf_loss = tf.reduce_mean(vf_losses1)
        # Calculate ratio (pi current policy / pi old policy)
        ratio = tf.exp(train_model.logp() - act_model.logp())

        # define the loss = -J is equivalent to max J
        pg_losses = -adv * ratio
        pg_losses2 = -adv * tf.clip_by_value(ratio, 1.0 - cliprange, 1.0 + cliprange)

        # Final pg loss
        pg_loss = tf.reduce_mean(tf.maximum(pg_losses, pg_losses2))
        # approxkl = .5 * tf.reduce_mean(tf.square(neglogpac - oldneglogpac))
        kloldnew = act_model.kl(train_model)
        approxkl = tf.reduce_mean(kloldnew)
        clipfrac = tf.reduce_mean(tf.to_float(tf.greater(tf.abs(ratio - 1.0), cliprange)))

        # total loss
        loss = pg_loss - entropy * ent_coef + vf_loss * vf_coef

        # Update the parameters using loss
        # 1. get the model parameters
        params = tf.trainable_variables('pi')

        # 2. Build our trainer
        trainer = MpiAdamOptimizer(MPI.COMM_WORLD, learning_rate=lr, epsilon=1e-5)

        # 3. Calculate the gradients
        grads_and_var = trainer.compute_gradients(loss, params)
        grads, var = zip(*grads_and_var)

        if max_grad_norm is not None:
            # Clip the gradients (normalize)
            grads, _grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        grads_and_var = list(zip(grads, var))
        # zip aggregate each gradient with parameters associated
        # For instance zip(ABCD, xyza) => Ax, By, Cz, Da

        _train = trainer.apply_gradients(grads_and_var)

        # decoder_input action_length is speciallized for the training model
        def train(learning_reate, clipingrange, input_obs,
                  returns, advs, actions, values, neglogpacs, states=None):
            # the advantage function is calculated as A(s,a) = R + yV(s') - V(s)
            # the return = R + yV(s')

            # Sequential Normalize the advantages
            advs = (advs - np.mean(advs, axis=0)) / (np.std(advs, axis=0) + 1e-8)
            input_obs = np.array(input_obs)
            actions = np.array(actions)
            returns = np.array(returns)


            td_map = {obs: input_obs, action: actions,  adv: advs, ret: returns, lr: learning_reate,
                      cliprange: clipingrange, oldneglogpac: neglogpacs, oldvpred: values}

            return sess.run([pg_loss, vf_loss, entropy, approxkl, clipfrac, _train], td_map)[:-1]

        self.loss_names = ['policy_loss', 'value_loss', 'policy_entropy', 'approxkl', 'clipfrac']

        self.train = train
        self.train_model = train_model
        self.act_model = act_model
        self.step = act_model.step
        self.greedy_predict = act_model.greedy_predict

        self.save = functools.partial(save_variables, sess=sess)
        self.load = functools.partial(load_variables, sess=sess)
        self.assign_old_eq_new = assign_old_eq_new

        if MPI.COMM_WORLD.Get_rank() == 0:
            initialize()
        global_variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES, scope="")
        sync_from_root(sess, global_variables)


class Runner():
    def __init__(self, env, model, nepisode, gamma, lam):
        self.lam = lam
        self.gamma = gamma
        self.model = model
        self.nepisode = nepisode
        self.env = env

    def run(self):
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones, mb_neglogpacs = [], [], [], [], [], []
        mb_tdlamret, mb_adv = [], []
        mb_decoder_input = []
        mb_decoder_length = []
        mb_encoder_batch = []
        mb_encoder_length = []
        mb_task_graph = []


        for task_graph_batch, encoder_batch, encoder_length, \
            decoder_lengths,  max_running_time, min_running_time in zip(self.env.task_graphs,
                                                                  self.env.encoder_batchs,
                                                                  self.env.encoder_lengths,
                                                                  self.env.decoder_full_lengths,
                                                                  self.env.max_running_time_batchs,
                                                                  self.env.min_running_time_batchs):
            for _ in range(self.nepisode):
                actions, values, neglogpacs = self.model.step(obs=encoder_batch)

                mb_encoder_batch += encoder_batch.tolist()
                mb_encoder_length += encoder_length.tolist()
                actions = np.array(actions)
                values = np.array(values)
                neglogpacs = np.array(neglogpacs)

                decoder_input = np.column_stack(
                    (np.ones(actions.shape[0], dtype=int) * self.env.start_symbol, actions[:, 0:-1]))
                mb_decoder_input += decoder_input.tolist()
                mb_decoder_length += decoder_lengths.tolist()
                mb_actions += actions.tolist()
                mb_values += values.tolist()
                mb_neglogpacs += neglogpacs.tolist()

                rewards = self.env.step(task_graph_batch=task_graph_batch, action_sequence_batch=actions,
                                        max_running_time_batch=max_running_time, min_running_time_batch=min_running_time)

                mb_rewards += rewards.tolist()
                mb_task_graph += task_graph_batch

                time_length = values.shape[1]
                batch_size = values.shape[0]
                vpred_batch = np.column_stack((values, np.zeros(batch_size, dtype=float)))
                last_gae_lam = np.zeros(batch_size, dtype=float)
                tdlamret = []
                adv = []

                for t in reversed(range(time_length)):
                    delta = rewards[:, t] + self.gamma * vpred_batch[:, t + 1] - vpred_batch[:, t]
                    gaelam = last_gae_lam = delta + self.gamma * self.lam * last_gae_lam
                    adv.append(gaelam)
                    tdlam = vpred_batch[:, t + 1] + gaelam
                    tdlamret.append(tdlam)

                tdlamret.reverse()
                adv.reverse()

                tdlamret = np.array(tdlamret).swapaxes(0, 1)
                adv = np.array(adv).swapaxes(0, 1)

                mb_tdlamret += tdlamret.tolist()
                mb_adv += adv.tolist()

        # return the trajectories
        return mb_encoder_batch, mb_encoder_length, mb_decoder_input, \
               mb_actions, mb_decoder_length, mb_values, mb_rewards, mb_neglogpacs, mb_tdlamret, mb_adv

    def sample_eval(self):
        running_cost = []
        energy_consumption = []
        running_qoe = []

        for encoder_batch, encoder_length, decoder_lengths, task_graph_batch \
                                                            in zip(self.env.encoder_batchs, self.env.encoder_lengths,
                                                                  self.env.decoder_full_lengths, self.env.task_graphs):

            actions, values, neglogpacs = self.model.step(obs=encoder_batch)
            actions = np.array(actions)
            env_running_cost, env_energy_consumption = self.env.get_running_cost(action_sequence_batch=actions,
                                                         task_graph_batch=task_graph_batch)

            qoe = calculate_qoe(env_running_cost, env_energy_consumption, self.env)

            running_cost += env_running_cost
            energy_consumption += env_energy_consumption
            running_qoe += qoe
        return running_cost, energy_consumption, running_qoe

    def greedy_eval(self):
        running_cost = []
        energy_consumption = []
        running_qoe = []

        for encoder_batch, encoder_length, decoder_lengths, task_graph_batch \
                in zip(self.env.encoder_batchs, self.env.encoder_lengths,
                       self.env.decoder_full_lengths, self.env.task_graphs):
            actions = self.model.greedy_predict(obs=encoder_batch)

            actions = np.array(actions)

            env_running_cost, env_energy_consumption = self.env.get_running_cost(action_sequence_batch=actions,
                                                         task_graph_batch=task_graph_batch)

            env_qoe = calculate_qoe(env_running_cost, env_energy_consumption, self.env)

            running_cost += env_running_cost
            energy_consumption += env_energy_consumption
            running_qoe += env_qoe
        return running_cost, energy_consumption, env_qoe


# the main ppo learning method
def learn(network, env, total_timesteps, eval_envs = None, seed=None, nupdates=1000, nsample_episode=30, nsteps=2048, ent_coef=0.01, lr=1e-4,
          vf_coef=0.5, max_grad_norm=0.5, gamma=0.99, lam=0.95, optbatchnumber=500,
          log_interval=1, nminibatches=4, noptepochs=4, cliprange=0.2,
          save_interval=0, load_path=None, **network_kwargs):

    #policy = build_policy(env, network, hparameters=hparams)

    make_model = lambda: ANNPPOModel(obs_dim=env.input_dim, action_dim=2, hidden_units=256,
                                     ent_coef=ent_coef, vf_coef=vf_coef, max_grad_norm=max_grad_norm)

    model = make_model()
    if load_path is not None:
        model.load(load_path)
    runner = Runner(env=env, model = model, nepisode=nsample_episode, gamma=gamma, lam=lam)

    eval_runners = []
    if eval_envs is not None:
        for eval_env in eval_envs:
            eval_runners.append(Runner(env=eval_env, model=model, nepisode=1, gamma=gamma, lam=lam))

    # define the saver

    # Start total timer
    tfirststart = time.time()

    mean_reward_track = []
    for update in range(1, nupdates+1):
        tstart = time.time()
        # Get the learning rate
        lrnow = lr
        # Get the clip range
        cliprangenow = cliprange
        # Get minibatchs

        mb_encoder_batch, mb_encoder_length, mb_decoder_input, mb_actions, mb_decoder_length, \
        mb_values, mb_rewards, mb_neglogpacs, mb_tdlamret, mb_adv = runner.run()

        sample_time_cost = time.time()
        print("sample time cost: ", (sample_time_cost - tstart))
        print(np.array(mb_encoder_batch).shape)

        data_set = Dataset(dict(encoder_input=mb_encoder_batch,
                                encoder_length=mb_encoder_length,
                                decoder_input=mb_decoder_input,
                                decoder_target=mb_actions,
                                decoder_full_length=mb_decoder_length,
                                returns=mb_tdlamret,
                                advs =mb_adv,
                                values=mb_values,
                                neglogpacs=mb_neglogpacs),
                                deterministic=True, shuffle=False)

        mean_reward = np.mean(np.sum(mb_rewards, axis=-1))

        mblossvals = []

        # optimal policy update steps
        logger.log(fmt_row(13, model.loss_names))
        for _ in range(noptepochs):
            for batch in data_set.iterate_once(optbatchnumber):
                encoder_input = batch["encoder_input"]
                returns_batch = batch["returns"]
                advs_batch = batch["advs"]
                decoder_target = batch["decoder_target"]
                values_batch = batch["values"]
                neglogpacs_batch = batch["neglogpacs"]

                batch_loss = model.train(
                                learning_reate=lrnow,
                                clipingrange=cliprangenow,
                                input_obs=encoder_input,
                                returns=returns_batch,
                                advs=advs_batch,
                                actions=decoder_target,
                                values=values_batch,
                                neglogpacs=neglogpacs_batch)
                mblossvals.append(batch_loss)
            logger.log(fmt_row(13, np.mean(mblossvals, axis=0)))

        update_time_cost = time.time()
        print("Update time cost: ", (update_time_cost-sample_time_cost))

        # sychronous the paramters:
        model.assign_old_eq_new()
        assign_params_time_cost = time.time()
        print("Sychronous parameters cost: ", (assign_params_time_cost - update_time_cost))

        running_cost = []
        energy_consumption = []
        running_qoe = []

        greedy_running_cost = []
        greedy_energy_consumption = []
        greedy_qoe_batch = []
        for eval_runner in eval_runners:
            Tc, Ec, qoe = eval_runner.sample_eval()
            greedy_Tc, greedy_Ec, greedy_qoe = eval_runner.greedy_eval()

            Tc = np.mean(Tc)
            Ec = np.mean(Ec)
            qoe = np.mean(qoe)

            greedy_Tc = np.mean(greedy_Tc)
            greedy_Ec = np.mean(greedy_Ec)
            greedy_qoe = np.mean(greedy_qoe)

            running_cost.append(Tc)
            energy_consumption.append(Ec)
            running_qoe.append(qoe)

            greedy_running_cost.append(greedy_Tc)
            greedy_energy_consumption.append(greedy_Ec)
            greedy_qoe_batch.append(greedy_qoe)

        lossvals = np.mean(mblossvals, axis=0)
        # End timer
        tnow = time.time()

        mean_reward_track.append(mean_reward)
        if update % log_interval == 0 or update == 1:
            # save model
            model.save("./checkpoint/model.ckpt")
            print("model saved!")

            # Calculates if value function is a good predicator of the returns (ev > 1)
            # or if it's just worse than predicting nothing (ev =< 0)
            logger.logkv('time_step', update)
            logger.logkv('time_elapsed', tnow - tfirststart)
            logger.logkv('time_one_episode', tnow - tstart)

            j = 0
            for eval_env, run_time, energy, running_mean_qoe, greedy_run_time, greedy_energy, greedy_mean_qoe in zip(eval_envs, running_cost,
                                                                                 energy_consumption,
                                                                                 running_qoe,
                                                                                 greedy_running_cost,
                                                                                 greedy_energy_consumption,
                                                                                  greedy_qoe_batch):
                logger.logkv(str(j)+'th run time cost ', run_time)
                logger.logkv(str(j)+'th energy cost ', energy)
                logger.logkv(str(j)+'th qoe ', running_mean_qoe)

                logger.logkv(str(j)+'th greedy run time cost', greedy_run_time)
                logger.logkv(str(j)+'th greedy energy cost', greedy_energy)
                logger.logkv(str(j) + 'th greedy qoe', greedy_mean_qoe)

                logger.logkv(str(j) + 'th HEFT run time cost', eval_env.heft_avg_run_time)
                logger.logkv(str(j) + 'th HEFT energy cost', eval_env.heft_avg_energy)
                logger.logkv(str(j) + 'th HEFT qoe', eval_env.heft_avg_qoe)
                j += 1

            #logger.logkv('optimal run time cost', eval_env.optimal_solution[0])
            logger.logkv('mean reward', mean_reward)

            for (lossval, lossname) in zip(lossvals, model.loss_names):
                logger.logkv(lossname, lossval)

            if MPI.COMM_WORLD.Get_rank() == 0:
                logger.dumpkvs()

    return mean_reward_track


if __name__ == "__main__":
    lambda_t = 1.0
    lambda_e = 0.0

    logger.configure('./log/all-graph-no-dependency-latency-optimal', ['stdout', 'json', 'csv'])

    hparams = tf.contrib.training.HParams(
        unit_type="layer_norm_lstm",
        num_units=256,
        learning_rate=0.00005,
        supervised_learning_rate=0.00005,
        n_features=2,
        time_major=False,
        is_attention=True,
        forget_bias=1.0,
        dropout=0,
        num_gpus=1,
        num_layers=2,
        num_residual_layers=0,
        is_greedy=False,
        inference_model="sample",
        start_token=0,
        end_token=5,
        is_bidencoder=True
    )

    resource_cluster = Resources(mec_process_capable=(10.0 * 1024 * 1024),
                                 mobile_process_capable=(1.0 * 1024 * 1024), bandwith_up=7.0, bandwith_dl=7.0)

    env = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=500, graph_number=500,
                                graph_file_paths=["./RLWorkflow/offloading_data/offload_random10/random.10.",
                                                  "./RLWorkflow/offloading_data/offload_random15/random.15.",
                                                  "./RLWorkflow/offloading_data/offload_random20/random.20.",
                                                  "./RLWorkflow/offloading_data/offload_random25/random.25.",
                                                  "./RLWorkflow/offloading_data/offload_random30/random.30.",
                                                  "./RLWorkflow/offloading_data/offload_random35/random.35.",
                                                  "./RLWorkflow/offloading_data/offload_random40/random.40.",
                                                  "./RLWorkflow/offloading_data/offload_random45/random.45.",
                                                  "./RLWorkflow/offloading_data/offload_random50/random.50.",
                                                  ],
                                time_major=False,
                                lambda_t=lambda_t,
                                lambda_e=lambda_e,
                                encode_dependencies=False)

    # env.calculate_optimal_solution()
    eval_envs = []
    eval_env_1 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random10_test/random.10."],
                                       time_major=False,
                                       lambda_t=lambda_t,
                                       lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_1.calculate_heft_cost()

    eval_envs.append(eval_env_1)

    eval_env_2 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random15_test/random.15."],
                                       time_major=False,
                                       lambda_t=lambda_t, lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_2.calculate_heft_cost()

    eval_envs.append(eval_env_2)

    eval_env_3 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random20_test/random.20."],
                                       time_major=False,
                                       lambda_t=lambda_t, lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_3.calculate_heft_cost()
    eval_envs.append(eval_env_3)

    eval_env_4 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random25_test/random.25."],
                                       time_major=False,
                                       lambda_t=lambda_t, lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_4.calculate_heft_cost()

    eval_envs.append(eval_env_4)

    eval_env_5 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random30_test/random.30."],
                                       time_major=False,
                                       lambda_t=lambda_t, lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_5.calculate_heft_cost()

    eval_envs.append(eval_env_5)

    eval_env_6 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random35_test/random.35."],
                                       time_major=False,
                                       lambda_t=lambda_t, lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_6.calculate_heft_cost()
    eval_envs.append(eval_env_6)

    eval_env_7 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random40_test/random.40."],
                                       time_major=False,
                                       lambda_t=lambda_t, lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_7.calculate_heft_cost()
    eval_envs.append(eval_env_7)

    eval_env_8 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random45_test/random.45."],
                                       time_major=False,
                                       lambda_t=lambda_t, lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_8.calculate_heft_cost()
    eval_envs.append(eval_env_8)

    eval_env_9 = OffloadingEnvironment(resource_cluster=resource_cluster, batch_size=100, graph_number=100,
                                       graph_file_paths=[
                                           "./RLWorkflow/offloading_data/offload_random50_test/random.50."],
                                       time_major=False,
                                       lambda_t=lambda_t, lambda_e=lambda_e,
                                       encode_dependencies=False)
    eval_env_9.calculate_heft_cost()
    eval_envs.append(eval_env_9)
    print("Finishing initialization of environment")

    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        mean_reward_track = learn(network="default", env=env, eval_envs=eval_envs, nsample_episode=10, nupdates=3000,
                                  max_grad_norm=1.0, noptepochs=4, gamma=0.99,
                                  total_timesteps=80000, lr=5e-4, optbatchnumber=500)

    x = np.arange(0, len(mean_reward_track), 1)

    print("Maxmium episode reward is {}".format(np.max(mean_reward_track)))

    import matplotlib.pyplot as plt

    plt.plot(x, mean_reward_track)
    plt.xlabel('episode')
    plt.ylabel('reward')
    plt.show()





