"""
An example of Entropy Regularised Policy Distillation
according to https://arxiv.org/abs/1902.02186
"""
import sys
sys.path.append('../')
import argparse
import json
import os
from pprint import pprint
import pdb
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import gym
import pybullet as p
import machina as mc
from machina.pols import GaussianPol, CategoricalPol, MultiCategoricalPol
from machina.algos import entropy_regularised
from machina.vfuncs import DeterministicSVfunc
from machina.envs import GymEnv, C2DEnv
from machina.traj import Traj
from machina.traj import epi_functional as ef
from machina.samplers import EpiSampler
from machina import logger
from machina.utils import measure, set_device
from simple_net import PolNet, VNet, PolNetLSTM, VNetLSTM

parser = argparse.ArgumentParser()
parser.add_argument('--teacher_dir', type=str, default='teacher_pol_pendulum',
                    help='Directory path storing file of expert policy model')
parser.add_argument('--teacher_fname', type=str,
                    default='/home/pierre/machina/example/teacher_pol_pendulum/models/pol_max.pkl', help='File name of expert policy model')
parser.add_argument('--log', type = str, default = 'student_garbage',
                    help = 'Directory path storing results of student model')
parser.add_argument('--env_name', type=str,
                    default='Pendulum-v0', help='Name of environment')
parser.add_argument('--c2d', action='store_true',
                    default=False, help='If True, action is discretized')
parser.add_argument('--record', action='store_true',
                    default=False, help='If True, movie is saved')
parser.add_argument('--seed', type=int, default=256)
parser.add_argument('--max_epis', type=int,
                    default=1000000, help='Number of episodes to run')
parser.add_argument('--num_parallel', type=int, default=4,
                    help='Number of processes used to sample')
parser.add_argument('--cuda', type=int, default=-1, help='Cuda device number')
parser.add_argument('--max_steps_per_iter', type=int,
                    default=10000, help='Maximum steps per iteration')
parser.add_argument('--max_epis_per_iter', type=int,
                    default=256, help='Maximum episodes per iteration')
parser.add_argument('--epoch_per_iter', type=int, default=10,
                    help='Number of epochs per optimization iteration')
parser.add_argument('--batch_size', type=int, default=256)
parser.add_argument('--gamma', type=float, default=0.995)
parser.add_argument('--lam', type=float, default=1)
parser.add_argument('--rnn', action='store_true', default=False,
                    help='If True, network becomes recurrent')
parser.add_argument('--pol_lr', type=float, default=3e-4,
                    help='Learning rate of the optimizer')
args = parser.parse_args()

if not os.path.exists(args.log):
    os.mkdir(args.log)

with open(os.path.join(args.log, 'args.json'), 'w') as f:
    json.dump(vars(args), f)
pprint(vars(args))

if not os.path.exists(os.path.join(args.log, 'models')):
    os.mkdir(os.path.join(args.log, 'models'))

np.random.seed(args.seed)
torch.manual_seed(args.seed)

device_name = 'cpu' if args.cuda < 0 else "cuda:{}".format(args.cuda)
device = torch.device(device_name)
set_device(device)

score_file = os.path.join(args.log, 'progress.csv')
logger.add_tabular_output(score_file)

env = GymEnv(
    args.env_name,
    log_dir=os.path.join(
        args.log,
        'movie'),
    record_video=args.record)
env.env.seed(args.seed)
if args.c2d:
    env = C2DEnv(env)

ob_space = env.observation_space
ac_space = env.action_space

# Generate teacher (t) policy and student (s) policy and load teacher policy
# Please note that the two policies do not have to have the same hidden architecture

if args.rnn:
    t_pol_net = PolNetLSTM(ob_space, ac_space, h_size=256, cell_size=256)
    s_pol_net = PolNetLSTM(ob_space, ac_space, h_size=256, cell_size=256)
else:
    t_pol_net = PolNet(ob_space, ac_space)
    s_pol_net = PolNet(ob_space, ac_space, h1=190, h2=90)
if isinstance(ac_space, gym.spaces.Box):
    t_pol = GaussianPol(ob_space, ac_space, t_pol_net, args.rnn)
    s_pol = GaussianPol(ob_space, ac_space, s_pol_net, args.rnn)
elif isinstance(ac_space, gym.spaces.Discrete):
    t_pol = CategoricalPol(ob_space, ac_space, t_pol_net, args.rnn)
    s_pol = CategoricalPol(ob_space, ac_space, s_pol_net, args.rnn)
elif isinstance(ac_space, gym.spaces.MultiDiscrete):
    t_pol = MultiCategoricalPol(ob_space, ac_space, t_pol_net, args.rnn)
    s_pol = MultiCategoricalPol(ob_space, ac_space, s_pol_net, args.rnn)
else:
    raise ValueError('Only Box, Discrete and Multidiscrete are supported')

if args.teacher_fname is not '':
    t_pol.load_state_dict(torch.load(
        os.path.join(args.teacher_dir, args.teacher_fname)))
else:
    raise ValueError('Please provide a teacher-policy!')

if args.rnn:
    s_vf_net = VNetLSTM(ob_space, h_size=256, cell_size=256)
else:
    s_vf_net = VNet(ob_space)

student_sampler = EpiSampler(
    env,
    s_pol,
    num_parallel=args.num_parallel,
    seed=args.seed)

optim_pol = torch.optim.Adam(s_pol_net.parameters(), args.pol_lr)

total_epi = 0
total_step = 0
max_rew = -1e6

while args.max_epis > total_epi:
    with measure('sample'):
        epis = student_sampler.sample(
            s_pol, max_epis=args.max_epis_per_iter)
    with measure('train'):
        traj = Traj()
        traj.add_epis(epis)
        traj = ef.compute_h_masks(traj)
        traj = ef.compute_log_rews(traj, teacher_pol=t_pol)
        pdb.set_trace()
        traj.register_epis()
        result_dict = entropy_regularised.train(
            traj=traj,
            student_pol=s_pol,
            epoch=args.epoch_per_iter,
            batchsize=args.batch_size)

    logger.log('Testing Student-policy')
    with measure('sample'):
        epis_measure = student_sampler.sample(
            s_pol, max_epis=args.max_epis_per_iter)

    with measure('measure'):
        traj_measure = Traj()
        traj_measure.add_epis(epis_measure)
        traj_measure = ef.compute_h_masks(traj_measure)
        traj_measure.register_epis()

    total_epi += traj_measure.num_epi
    step = traj_measure.num_step
    total_step += step
    rewards = [np.sum(epi['rews']) for epi in epis_measure]
    mean_rew = np.mean(rewards)
    logger.record_results(args.log, result_dict, score_file,
                          total_epi, step, total_epi, rewards,
                          plot_title='Policy Distillation')

    del traj
    del traj_measure
del sampler