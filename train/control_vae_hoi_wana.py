import torch as th
import numpy as np
from torch import nn
import torch.distributions as D
from ControlVAECore.Model.trajectory_collection import TrajectorCollector
# from ControlVAECore.Model.world_model_hoi import SimpleWorldModel
from ControlVAECore.Model.world_model_hoi_wana import SimpleWorldModel
# from ControlVAECore.Model.world_model_hoi import SimpleWorldModel
from ControlVAECore.Model.world_model_hoi_wana_v2 import SimpleWorldModel as SimpleWorldModel_v2

import os 

from utils.motion_utils import pose_err_ours, pose_err_ours_twohands
from utils import pytorch_utils as ptu
# from utils import dist_util
from utils.replay_buffer_hoi import ReplayBuffer
from utils.mpi_utils import gather_dict_ndarray
from utils.radam import RAdam
from ControlVAECore.Model.modules import *
# from diffusion.resample import LossAwareSampler, UniformSampler, create_named_schedule_sampler
import blobfile as bf

import time
import sys
from mpi4py import MPI

import wandb

mpi_comm = MPI.COMM_WORLD
mpi_world_size = mpi_comm.Get_size()
mpi_rank = mpi_comm.Get_rank()



# it's true when it's not root process or there is only root process (no subprocess) 
should_do_subprocess_task = mpi_rank > 0 or mpi_world_size == 1



class PolicyAgent(nn.Module):
    def __init__(self, observation_size, action_size, delta_size, env, diffusion, **kargs):
        super().__init__()
        
        ptu.init_gpu(True, gpu_id=0)
        
        
        ## observations ##
        self.kargs = kargs
        
        self.wandb = kargs['wandb']
        self.traj_opt = kargs['traj_opt']
        self.nn_traj_samples = kargs['nn_traj_samples']
        self.reset_mano_states = kargs['reset_mano_states']
        self.use_mano_delta_states = kargs['use_mano_delta_states']
        
        self.use_cmaes = kargs['use_cmaes']
        
        self.two_hands = kargs['two_hands']
        
        if 'train_policy_only' in kargs:
            self.train_policy_only =kargs['train_policy_only']
        else:
            self.train_policy_only = False
            
        if 'load_ckpt' in kargs:
            self.load_ckpt = kargs['load_ckpt']
        else:
            self.load_ckpt = ""
            
        if 'train_visualfeats_policy' in kargs:
            self.visualfeats_policy = kargs['train_visualfeats_policy']
        else:
            self.visualfeats_policy = False
        self.train_visualfeats_policy = self.visualfeats_policy
        
        if 'use_base_sim_mpc' in kargs:
            self.use_base_sim_mpc = kargs['use_base_sim_mpc']
        else:
            self.use_base_sim_mpc = False

        
        print(f"train_visualfeats_policy: {self.train_visualfeats_policy}")
        
        
        mano_hand_mean_meshcoll_urdf_fn = kargs['mano_urdf_fn']
    
        hand_model_name = mano_hand_mean_meshcoll_urdf_fn.split("/")[-1].split(".")[0]
        if "mano" in hand_model_name:
            self.hand_type = "mano"
        elif "shadow" in hand_model_name:
            self.hand_type = "shadow"
        else:
            raise NotImplementedError(f"Unknown hand type: {hand_model_name}")
    
        
        self.gt_data_fn = kargs['sv_gt_refereces_fn']
        print(f"Loading from {self.gt_data_fn}")
        self.gt_data = np.load(self.gt_data_fn, allow_pickle=True).item()
        
        
        self.mano_glb_rot = self.gt_data['mano_glb_rot']
        self.mano_glb_rot = self.mano_glb_rot / np.clip(np.sqrt(np.sum(self.mano_glb_rot**2, axis=-1, keepdims=True)), a_min=1e-5, a_max=None)
        
        self.mano_glb_trans = self.gt_data['mano_glb_trans']
        
        
        
        if self.hand_type == "mano":
            self.mano_states = self.gt_data['mano_states'][:, :]
        elif self.hand_type == "shadow":
            self.mano_states = self.gt_data['mano_states'][:, 2: ]
        
        self.mano_glb_rot = torch.from_numpy(self.mano_glb_rot).float().to(ptu.device)
        self.mano_glb_trans = torch.from_numpy(self.mano_glb_trans).float().to(ptu.device)
        self.mano_states = torch.from_numpy(self.mano_states).float().to(ptu.device)
        
        action_size = self.mano_glb_rot.size(1) + self.mano_glb_trans.size(1) + self.mano_states.size(1)
        
        self.tot_nn_frames = self.mano_glb_rot.size(0)
        self.action_size = action_size
        self.finger_states_size = self.mano_states.size(1)
        
        self.mano_delta_states = []
        for i_fr in range(self.mano_states.size(0)):
            if i_fr == 0:
                self.mano_delta_states.append(self.mano_states[i_fr, :])
            else:
                self.mano_delta_states.append(self.mano_states[i_fr, :] - self.mano_states[i_fr-1, :])
        self.mano_delta_states = torch.stack(self.mano_delta_states, dim=0)
        
        if self.reset_mano_states:
            if '30' in self.gt_data_fn:
                print(f"Resetting mano states...")
                self.mano_states[:, 56 - 6] = 3.0
                self.mano_states[:, 58 - 6] = 0.9
            elif '20' in self.gt_data_fn:
                print(f"Resetting mano states...")
                self.mano_states[:, 56 - 6] = -0.2
                self.mano_states[:, 55 - 6] = 0.0
                self.mano_states[:, 57 - 6] = 0.0 
            elif '25' in self.gt_data_fn: # 
                print(f"Resetting mano states...")
                self.mano_states[:, 58 - 6] = 0.0
                self.mano_states[:, 59 - 6] = 0.0
                self.mano_states[:, 60 - 6] = 0.0

        if self.two_hands:
            ## left manojglb rot ## 
            self.left_mano_glb_rot = self.gt_data['left_mano_glb_rot']
            self.left_mano_glb_rot = self.left_mano_glb_rot / np.clip(np.sqrt(np.sum(self.left_mano_glb_rot**2, axis=-1, keepdims=True)), a_min=1e-5, a_max=None)
            
            ### left mano glb trans ###
            self.left_mano_glb_trans = self.gt_data['left_mano_glb_trans']
            
            if self.hand_type == "mano":
                self.left_mano_states = self.gt_data['left_mano_states'][:, :]
            elif self.hand_type == "shadow":
                self.left_mano_states = self.gt_data['left_mano_states'][:, 2 :]
        
            self.left_mano_glb_trans = torch.from_numpy(self.left_mano_glb_trans).float().to(ptu.device)
            self.left_mano_glb_rot = torch.from_numpy(self.left_mano_glb_rot).float().to(ptu.device)
            self.left_mano_states = torch.from_numpy(self.left_mano_states).float().to(ptu.device)
            
            ## mano_glb_trans, mano_glb_rot, mano_states ##
            self.mano_glb_trans = torch.cat(
                [self.mano_glb_trans, self.left_mano_glb_trans], dim=-1
            )
            self.mano_glb_rot = torch.cat(
                [self.mano_glb_rot, self.left_mano_glb_rot], dim=-1 ## mano glb rot 
            )
            self.mano_states = torch.cat(
                [self.mano_states, self.left_mano_states], dim=-1 ### mano_states ###
            )


        self.observation_size = observation_size
        self.action_size = action_size
        self.delta_size = delta_size
        
        
        # kargs['conf_path'] = "/home/xueyi/diffsim/NeuS/confs/dyn_arctic_robohand_from_mano_model_rules_actions_f2_diffhand_v4.conf"
        # kargs['conf_path'] = "/home/xueyi/diffsim/NeuS/confs/dyn_arctic_robohand_from_mano_model_rules_actions_f2_diffhand_v4.conf"
        
        self.use_multi_ed = True
        
        self.encoder = SimpleLearnablePriorEncoder(
            input_size= self.observation_size,
            condition_size= self.observation_size,
            output_size= kargs['latent_size'],
            **kargs).to(ptu.device)
        self.agent = GatingMixedDecoder(
            condition_size=self.observation_size,
            output_size=self.action_size,
            **kargs
        ).to(ptu.device)
        
        
        self.glb_feat_cond_dim = 128
        
        
        
        if self.use_multi_ed:
            if self.visualfeats_policy:
                self.encoder_trans = VisualFeatsLearnablePriorEncoder(
                    input_size= self.observation_size,
                    condition_size= self.glb_feat_cond_dim,
                    output_size= kargs['latent_size'],
                    **kargs).to(ptu.device)
                self.agent_trans = GatingMixedDecoder(
                    # latent_size= kargs['latent_size'],
                    condition_size=self.observation_size,
                    output_size=3,
                    **kargs
                ).to(ptu.device)
                
                self.encoder_rot = VisualFeatsLearnablePriorEncoder(
                    input_size= self.observation_size,
                    condition_size= self.glb_feat_cond_dim,
                    output_size= kargs['latent_size'],
                    **kargs).to(ptu.device)
                self.agent_rot = GatingMixedDecoder(
                    # latent_size= kargs['latent_size'],
                    condition_size=self.observation_size,
                    output_size=3,
                    **kargs
                ).to(ptu.device)
                
                self.encoder_states = VisualFeatsLearnablePriorEncoder(
                    input_size= self.observation_size,
                    condition_size= self.glb_feat_cond_dim,
                    output_size= kargs['latent_size'],
                    # fix_var = kargs['encoder_fix_var'],
                    **kargs).to(ptu.device)
                
                self.agent_states = GatingMixedDecoder(
                    # latent_size= kargs['latent_size'],
                    condition_size=self.observation_size,
                    output_size=env.bullet_mano_num_joints - 6,
                    **kargs
                ).to(ptu.device)
                
            else:
                self.encoder_trans = SimpleLearnablePriorEncoder(
                    input_size= self.observation_size,
                    condition_size= self.observation_size,
                    output_size= kargs['latent_size'],
                    # fix_var = kargs['encoder_fix_var'],
                    **kargs).to(ptu.device)
                
                if self.traj_opt:
                    self.agent_trans = GatingMixedDecoderV2(
                        # latent_size= kargs['latent_size'],
                        condition_size=self.observation_size,
                        output_size=3,
                        nn_frames=self.mano_glb_trans.size(0),
                        nn_dim=self.mano_glb_trans.size(1),
                        frame_data=self.mano_glb_trans,
                        **kargs
                    ).to(ptu.device)
                else:
                    self.agent_trans = GatingMixedDecoder( # getting mixed decoder ## 
                        # latent_size= kargs['latent_size'],
                        condition_size=self.observation_size,
                        output_size=3,
                        **kargs
                    ).to(ptu.device)
                
                
                self.encoder_rot = SimpleLearnablePriorEncoder(
                    input_size= self.observation_size,
                    condition_size= self.observation_size,
                    output_size= kargs['latent_size'],
                    # fix_var = kargs['encoder_fix_var'],
                    **kargs).to(ptu.device)

                if self.traj_opt:
                    self.agent_rot = GatingMixedDecoderV2(
                        # latent_size= kargs['latent_size'],
                        condition_size=self.observation_size,
                        output_size=3,
                        nn_frames=self.mano_glb_rot.size(0),
                        nn_dim=self.mano_glb_rot.size(1),
                        frame_data=self.mano_glb_rot,
                        **kargs
                    ).to(ptu.device)
                else:
                    self.agent_rot = GatingMixedDecoder(
                        # latent_size= kargs['latent_size'],
                        condition_size=self.observation_size,
                        output_size=3,
                        **kargs
                    ).to(ptu.device)
                self.encoder_states = SimpleLearnablePriorEncoder(
                    input_size= self.observation_size,
                    condition_size= self.observation_size,
                    output_size= kargs['latent_size'],
                    # fix_var = kargs['encoder_fix_var'],
                    **kargs).to(ptu.device)
                if self.traj_opt:
                    if self.use_mano_delta_states:
                        self.agent_states = GatingMixedDecoderV2(
                            # latent_size= kargs['latent_size'],
                            condition_size=self.observation_size,
                            output_size=3,
                            nn_frames=self.mano_delta_states.size(0),
                            nn_dim=self.mano_delta_states.size(1),
                            frame_data=self.mano_delta_states,
                            is_delta=True,
                            **kargs
                        ).to(ptu.device)
                    else:
                        self.agent_states = GatingMixedDecoderV2(
                            # latent_size= kargs['latent_size'],
                            condition_size=self.observation_size,
                            output_size=3,
                            nn_frames=self.mano_states.size(0),
                            nn_dim=self.mano_states.size(1),
                            frame_data=self.mano_states,
                            **kargs
                        ).to(ptu.device)
                else:
                    self.agent_states = GatingMixedDecoder(
                        # latent_size= kargs['latent_size'],
                        condition_size=self.observation_size,
                        output_size=self.action_size - 6,
                        **kargs
                    ).to(ptu.device)
        
        self.diffusion = diffusion
        
        
        
        statistics = env.stastics
        self.obs_mean = nn.Parameter(ptu.from_numpy(statistics['obs_mean']), requires_grad = False).to(ptu.device)
        self.obs_std = nn.Parameter(ptu.from_numpy(statistics['obs_std']), requires_grad= False).to(ptu.device)
        
        
        print(f"Constructing world model")
        
        
        if kargs['wana'] and kargs['traj_opt']:
            self.world_model = SimpleWorldModel_v2(self.observation_size, self.action_size, self.delta_size, env.dt, statistics, env, **kargs).to(ptu.device)
        else:
            self.world_model = SimpleWorldModel(self.observation_size, self.action_size, self.delta_size, env.dt, statistics, **kargs).to(ptu.device)
        
        
        self.wm_optimizer = RAdam(self.world_model.parameters(), kargs['world_model_lr'], weight_decay=1e-3)
        
        if self.use_multi_ed:
            ### multiple encoder and decoder ###
            params_to_train = list(self.encoder_rot.parameters()) + list(self.agent_rot.parameters()) + list(self.encoder_trans.parameters()) + list(self.agent_trans.parameters()) + list(self.encoder_states.parameters()) + list(self.agent_states.parameters())
            # if self.two_hands:
            #     params_to_train += 
            self.policy_optimizer = RAdam(params_to_train, kargs['policy_lr'])
        else:
            self.policy_optimizer = RAdam(list(self.encoder.parameters()) + list(self.agent.parameters()), kargs['policy_lr'])
        # self.vae_optimizer = RAdam( list(self.encoder.parameters()) + list(self.agent.parameters()), kargs['controlvae_lr'])
        self.beta_scheduler = ptu.scheduler(0,8,0.009,0.09,500*8)
        
        
        
        self.best_policy_loss = 9999999.0
        self.cur_policy_loss =  9999999.0
        
        self.wnorm = kargs['wnorm']
        
        #hyperparameters.... ## witout normal 
        if self.wnorm:
            self.action_sigma = 0.05 # action 
        else:
            self.action_sigma = 0.0001 # action 
            self.trans_action_sigma = 0.0001
            self.rot_action_sigma = 0.001
            self.finger_state_action_sigma = 0.01
            
            self.action_sigma = 0.0001 # action 
            self.trans_action_sigma = 0.00001001
            self.rot_action_sigma = 0.0001
            self.finger_state_action_sigma = 0.0001
            
            
            self.trans_action_sigma = 0.0000001
            self.rot_action_sigma = 0.0000001

            if 'finger_state_action_sigma' in kargs:
                self.finger_state_action_sigma = float(kargs['finger_state_action_sigma'])
            else:
                self.finger_state_action_sigma = 0.01
        self.max_iteration = kargs['max_iteration'] # collect size ## ## collect size 
        self.collect_size = kargs['collect_size'] ## collect size ##
        self.sub_iter = kargs['sub_iter']
        self.save_period = kargs['save_period'] ## world model rollout length ## rollout length ##
        self.evaluate_period = kargs['evaluate_period']  # evaluate period # save period #
        self.world_model_rollout_length = kargs['world_model_rollout_length'] # policy rollout length #
        self.policy_rollout_length = kargs['policy_rollout_length']
        self.world_model_batch_size = kargs['world_model_batch_size']
        self.policy_batch_size = kargs['policy_batch_size']
        
        self.bullet_nn_substeps = kargs['bullet_nn_substeps']
        self.use_ana = kargs['use_ana']
        
        
        if 'train_wm_only' in kargs:
            self.train_wm_only = kargs['train_wm_only']
        else:
            self.train_wm_only = False
        
        
        if kargs['wana'] or kargs['use_ana']:
            print(f"with wana!")
            self.world_model_batch_size = 1
            self.policy_batch_size = 1
        self.save_dir = kargs['save_dir']
        
        if not os.path.exists(self.save_dir):
            os.mkdir(self.save_dir)
        self.tag = kargs['tag']
        self.save_dir = os.path.join(self.save_dir, self.tag)
        os.makedirs(self.save_dir, exist_ok=True)
        
        self.wm_logging_dir = os.path.join(
            self.save_dir, "wm_logging"
        )
        os.makedirs(self.wm_logging_dir, exist_ok=True)
        
        ### get loss weights ##
        obj_tar_weight = 1.0
        obj_tar_weight = 5.0
        self.weight = {}
        for key,value in kargs.items():
            if 'policy_weight' in key: # policy weight # ## policy weight ## ## policy 
                self.weight[key.replace('policy_weight_','')] = value
        for k in ['mano_trans', 'mano_rot', 'mano_states', 'obj_rot', 'obj_trans']:
            self.weight[f'weight_{k}'] =  obj_tar_weight # 1.0
        
        ## weight ##
        mano_tar_weight = 0.1 ## 
        mano_tar_weight = 1.0 ## perhaps it jshould be tuned based on the qualty of mano targets ## ## mano targets ## 
        self.weight[f'weight_mano_states'] = mano_tar_weight
        self.weight[f'weight_mano_rot'] = mano_tar_weight
        self.weight[f'weight_mano_trans'] = mano_tar_weight
        
        
        # for real trajectory collection
        self.runner = TrajectorCollector(venv = env, actor = self, runner_with_noise = True, use_ana = self.use_ana)
        self.env = env    
        self.replay_buffer = ReplayBuffer(self.replay_buffer_keys, kargs['replay_buffer_size']) if mpi_rank ==0 else None
        self.kargs = kargs
        
        self.i_step = 0
        
        if len(self.load_ckpt) > 0 and os.path.exists(self.load_ckpt):
            self.try_load(self.load_ckpt)
        
        
        
    def parameters_for_sample(self):
        '''
        this part will be synced using mpi for sampling, world model is not necessary
        '''
        sampling_params = {
            # 'policy': self.policy.state_dict()
            'encoder': self.encoder.state_dict(),
            'agent': self.agent.state_dict(),
            
        }
        if self.use_multi_ed:
            sampling_params['encoder_trans'] = self.encoder_trans.state_dict()
            sampling_params['agent_trans'] = self.agent_trans.state_dict()
            sampling_params['encoder_rot'] = self.encoder_rot.state_dict()
            sampling_params['agent_rot'] = self.agent_rot.state_dict()
            sampling_params['encoder_states'] = self.encoder_states.state_dict()
            sampling_params['agent_states'] = self.agent_states.state_dict()
        return sampling_params
        return {
            # 'policy': self.policy.state_dict()
            'encoder': self.encoder.state_dict(),
            'agent': self.agent.state_dict(),
        }
        
    def load_parameters_for_sample(self, dict):
        # self.policy.load_state_dict(dict['policy'])
        self.encoder.load_state_dict(dict['encoder'])
        self.agent.load_state_dict(dict['agent'])
        
        if self.use_multi_ed:
            self.encoder_trans.load_state_dict(dict['encoder_trans'])
            self.agent_trans.load_state_dict(dict['agent_trans'])
            self.encoder_trans.load_state_dict(dict['encoder_trans'])
            self.agent_rot.load_state_dict(dict['agent_rot'])
            self.encoder_states.load_state_dict(dict['encoder_states'])
            self.agent_states.load_state_dict(dict['encoder_trans'])
        
    @property
    def world_model_data_name(self):
        return ['state', 'action', 'frame_num']
    
    @property
    def policy_data_name(self):
        return ['state', 'target', 'frame_num']
    
    @property
    def replay_buffer_keys(self):
        return ['state', 'action', 'target', 'done', 'frame_num']
    
    
    def update_policy(self, ):
        import cma
        if mpi_rank == 0:
            def cmaes_func(x):
                print(f"one func eval")
                self.env.reset(0)
                xx_size = len(x)
                x = np.array(x)
                x = np.reshape(x, (self.tot_nn_frames, -1))
                tot_delta_states = x[:, 3:]
                tot_losses = []
                for i_fr in range(self.tot_nn_frames):
                    
                    cur_fr_x = x[i_fr, :]
                    cur_fr_trans = cur_fr_x[:3]
                    # cur_fr_states = cur_fr_x[3:]
                    cur_fr_rot = self.agent_rot.frame_data_embedding_layer.weight.data[i_fr].detach().cpu().numpy()
                    cur_fr_trans = self.agent_trans.frame_data_embedding_layer.weight.data[i_fr].detach().cpu().numpy()
                    
                    cur_fr_states = np.sum(tot_delta_states[:i_fr+1, :], axis=0)
                    
                    cur_fr_action = np.concatenate(
                        [cur_fr_trans, cur_fr_rot, cur_fr_states], axis=0
                    )
                    cur_fr_action = torch.from_numpy(cur_fr_action).float()
                    observation, cur_step_pd_control, reward, done, info  = self.env.step_core_new_wcontrol(cur_fr_action)
                    cur_fr_res_state = observation['state']
                    cur_fr_target = observation['target']
                    # state and the target #
                    # cur_fr_
                    cos_half_angle = np.sum(cur_fr_res_state[-4:] * cur_fr_target[-4:])
                    obj_rot_loss = 1.0 * (1. - cos_half_angle) 
                    obj_trans_loss = np.sum(
                        (cur_fr_res_state[-7:-4] - cur_fr_target[-7:-4])**2
                    )
                    cur_obj_loss = obj_rot_loss.item() + obj_trans_loss.item()
                    tot_losses.append(cur_obj_loss)
                tot_losses = sum(tot_losses)
                return tot_losses
                pass
                # print(f"x: {x}")
                # print(f"x: {x.shape}")
                # print(f"self.policy: {self.policy}")
                # print(f"self.policy: {self.policy.shape}")
                # print(f"self.policy: {self.policy.size()}")

            # delta_states = 
            
            delta_states = []
            for i_fr in range(self.agent_states.frame_data_embedding_layer.weight.data.size(0)):
                if i_fr == 0:
                    delta_states.append(self.agent_states.frame_data_embedding_layer.weight.data[i_fr])
                else:
                    delta_states.append(self.agent_states.frame_data_embedding_layer.weight.data[i_fr] - self.agent_states.frame_data_embedding_layer.weight.data[i_fr - 1])
            delta_states = torch.stack(delta_states, dim=0)
            trans_states = torch.cat(
                [self.agent_trans.frame_data_embedding_layer.weight.data, delta_states], dim=-1
            )
            trans_states = trans_states.detach().cpu().numpy()
            trans_states = np.reshape(trans_states, (-1,))
            sigma = 0.1
            # sigma = 0.05
            
            es = cma.CMAEvolutionStrategy(trans_states.tolist(), sigma)
            
            for _ in range(6):
                solutions = es.ask()
                es.tell(solutions, [cmaes_func(x) for x in solutions])
                es.logger.add()  
                es.disp()
            
            ## xopt, es ##
            # x_opt, es = cma.fmin2(cmaes_func, trans_states.tolist(), sigma)
            x_opt = es.result_pretty()[0]
            
            x_opt = torch.tensor(x_opt).float().cuda()
            x_opt = x_opt.contiguous().view(self.tot_nn_frames, -1)
            x_trans, x_states = x_opt[..., :3], x_opt[..., 3:]
            # self.mano_glb_trans.data[:, :] = x_trans
            
            tot_states = []
            for i_fr in range(x_states.size(0)):
                if i_fr == 0:
                    tot_states.append(x_states[i_fr, :])
                else:
                    cur_state = torch.sum(x_states[:i_fr+1, :], dim=0)
                    tot_states.append(cur_state)
            x_states = torch.stack(tot_states, dim=0)
            
            self.mano_states.data[:, :] = x_states
            # self.agent_trans.
            # self.agent_trans.frame_data_embedding_layer.weight.data[:, :] = x_trans.clone()
            self.agent_states.frame_data_embedding_layer.weight.data[:, :] = x_states.clone()
    
        # res = gather_dict_ndarray(path)
        if mpi_rank == 0:
            ### parameters for sample ###  # 
            paramter = self.parameters_for_sample() 
            mpi_comm.bcast(paramter, root = 0) # 
            # self.replay_buffer.add_trajectory(res)
            # info = {
            #     'rwd_mean': np.mean(res['rwd']),
            #     'rwd_std': np.std(res['rwd']),
            #     'episode_length': len(res['rwd'])/(res['done']!=0).sum()
            # }
        else:
            paramter = mpi_comm.bcast(None, root = 0)
            self.load_parameters_for_sample(paramter)    


    def train_one_step(self):
        
        time1 = time.perf_counter()
        
        ##### evaluate the world model and save ######
        # with torch.no_grad():
        #     eval_wm_res = self.eval_world_model() # eval the world model ##
        #     eval_wm_rollout_traj = eval_wm_res['cur_rollout_sv_info']
        #     eval_wm_rollout_traj_sv_fn = os.path.join(self.wm_logging_dir, f"eval_rollout_batch_sv_dict_{self.i_step}.npy")
        #     np.save(eval_wm_rollout_traj_sv_fn, eval_wm_rollout_traj)
        #     print(f"eval wm rollout dict saved to {eval_wm_rollout_traj_sv_fn}")
        ##### evaluate the world model and save ######
        
        ## use glboal policy update the policy ##
        
        if self.use_cmaes:
            self.update_policy()
        
        if mpi_rank ==  0:
            ## mpi sync 
            evalulated_traj = self.runner.eval_one_traj(self) 
            # tag = f"mano_weight_001_cosrotloss_manotar_multied_{self.use_multi_ed}_lgobj_trajlen512_grabcamera_Trot_eu_lmass_ns_{self.bullet_nn_substeps}_bactv2_obm100_" 
            eval_sv_fn = f"evalulated_traj_sm_l512_wana_v3_{self.tag}_step_{self.i_step}_afcames.npy"
            
            eval_sv_fn = os.path.join(self.save_dir, eval_sv_fn)
            np.save(eval_sv_fn, evalulated_traj)
            print(f"evaluated saved to {eval_sv_fn}")
        
        if( not self.use_ana) and (not self.train_policy_only) :
            
            name_list = self.world_model_data_name
            rollout_length = self.world_model_rollout_length
            data_loader = self.replay_buffer.generate_data_loader(name_list, 
                                rollout_length+1,
                                self.world_model_batch_size, 
                                self.sub_iter)
            batch_idx = 0
            for batch in  data_loader:
                world_model_log = self.train_world_model(*batch)
                
                
                batch_idx += 1
        ## world model log ##
        time2 = time.perf_counter()
        
        if self.train_wm_only:
            return world_model_log
        
        
        if mpi_rank == 0:
            # evalulated_traj = self.runner.eval_one_traj(self) ## eval one traj ##
            # ## a dict with everything ##
            # eval_sv_fn = "evalulated_traj_sm_l512_wana.npy"
            # np.save(eval_sv_fn, evalulated_traj)
            # print(f"evaluated saved to {eval_sv_fn}")
            # if self.cur_policy_loss < self.best_policy_loss: ## train one step ##
            #     self.best_policy_loss = self.cur_policy_loss #
            #     best_eval_sv_fn = "evalulated_traj_sm_l512_best_wana.npy" #
            #     np.save(best_eval_sv_fn, evalulated_traj) # mpc traj sv fn #
            #     print(f"best evaluated saved to {best_eval_sv_fn} with policy loss: {self.best_policy_loss}") #
            
            
            ################# mpc eval #################
            targets_all_frames = self.env.get_tot_targets()
            targets_all_frames = torch.from_numpy(targets_all_frames).float().cuda()
            frame_nums = [_ for _ in range(1, self.env.frame_length + 1)]
            frame_nums = torch.tensor(frame_nums).long().cuda() ### frame_nums ###
            
            if (not self.train_wm_only) and (not self.train_visualfeats_policy) :
                mpc_traj = self.mpc_eval(targets_all_frames, frame_nums)
                mpc_traj_sv_fn = f"evalulated_mpc_traj_sm_l512_wana_v3_{self.tag}_step_{self.i_step}.npy"
                
                mpc_traj_sv_fn = os.path.join(self.save_dir, mpc_traj_sv_fn)
                
                np.save(mpc_traj_sv_fn, mpc_traj)
                print(f"MPC traj saved to {mpc_traj_sv_fn}")
                ################# mpc eval #################
            
            ## mpi sync ##
            evalulated_traj = self.runner.eval_one_traj(self)
            # tag = f"mano_weight_001_cosrotloss_manotar_multied_{self.use_multi_ed}_lgobj_trajlen512_grabcamera_Trot_eu_lmass_ns_{self.bullet_nn_substeps}_bactv2_obm100_" 
            eval_sv_fn = f"evalulated_traj_sm_l512_wana_v3_{self.tag}_step_{self.i_step}_afwm.npy"
            
            eval_sv_fn = os.path.join(self.save_dir, eval_sv_fn)
            np.save(eval_sv_fn, evalulated_traj)
            print(f"evaluated saved to {eval_sv_fn}")
        
        
            
        
        ## policy -> or the sampled rul ##
        print(f"Start training policy")
        name_list = self.policy_data_name
        rollout_length = self.policy_rollout_length # generate the data loader ##
        data_loader = self.replay_buffer.generate_data_loader(name_list, # 
                            rollout_length, 
                            self.policy_batch_size, 
                            self.sub_iter)

        tot_states = []
        tot_states_after_ana = []
        tot_states_targets = []
        tot_states_states = []
        for batch in data_loader: # batch in data loader # batch in data loader #
            # print(f"[train polic] with batch_jkeys: {batch.keys()}")
            # for k in batch: # policy or world model #
            #     print(f"k: {k}, val: {batch[k].size()}")
            policy_log = self.train_policy(*batch)
            
            # res['tot_states'] = tot_states
            # res['tot_states_after_ana'] = tot_states_after_ana
            # res['targets'] = targets.detach().cpu().numpy()
            # res['states'] = states.detach().cpu().numpy() ## train the policy ##
            tot_states.append(policy_log['tot_states'])
            try:    
                tot_states_after_ana.append(policy_log['tot_states_after_ana'])
            except:
                tot_states_after_ana = []
                pass
            tot_states_targets.append(policy_log['targets'])
            tot_states_states.append(policy_log['states'])
            
        
        # tot_states = np.concatenate(tot_states, axis=0)
        # tot_states_after_ana = np.concatenate(tot_states_after_ana, axis=0)
        
        sv_states_dict = {
            'tot_states': tot_states,
            'tot_states_after_ana': tot_states_after_ana,
            'tot_states_targets': tot_states_targets,
            'tot_states_states': tot_states_states,
        }
        sv_states_fn = f"world_model_stats_pred_step_{self.i_step}.npy"
        sv_states_fn = os.path.join(self.save_dir, sv_states_fn)
        np.save(sv_states_fn, sv_states_dict)
        print(f"world model states predictions saved to {sv_states_fn}")
        
        
        # log training time... #  training time #
        time3 = time.perf_counter()    
        if (not self.use_ana) and (not self.train_policy_only):     
            world_model_log['training_time'] = (time2 - time1)
        policy_log['training_time'] = (time3 - time2)
        if self.use_ana or self.train_policy_only:   
            return policy_log
        else:
            # merge the training log...
            return self.merge_dict([world_model_log, policy_log], ['WM','Policy'])
        
    def mpi_sync(self):
        
        if mpi_rank ==  0:
            # evalulated_traj = self.runner.eval_one_traj(self)
            # ## a dict with everything ##
            # eval_sv_fn = "evalulated_traj_sm_l512_wana.npy"
            # np.save(eval_sv_fn, evalulated_traj)
            # print(f"evaluated saved to {eval_sv_fn}")
            # if self.cur_policy_loss < self.best_policy_loss: ## train one step ##
            #     self.best_policy_loss = self.cur_policy_loss # 
            #     best_eval_sv_fn = "evalulated_traj_sm_l512_best_wana.npy" # 
            #     np.save(best_eval_sv_fn, evalulated_traj) # 
            #     print(f"best evaluated saved to {best_eval_sv_fn} with policy loss: {self.best_policy_loss}")
            
            ## mpi sync ##
            evalulated_traj = self.runner.eval_one_traj(self)
            # tag = f"mano_weight_001_cosrotloss_manotar_multied_{self.use_multi_ed}_lgobj_trajlen512_grabcamera_Trot_eu_lmass_ns_{self.bullet_nn_substeps}_bactv2_obm100_" 
            eval_sv_fn = f"evalulated_traj_sm_l512_wana_v3_{self.tag}_step_{self.i_step}.npy"
            
            eval_sv_fn = os.path.join(self.save_dir, eval_sv_fn)
            np.save(eval_sv_fn, evalulated_traj)
            print(f"evaluated saved to {eval_sv_fn}")
            
            # evaluated 
            evalulated_traj_enum_st = self.runner.eval_multi_traj_enum_st(self)
            eval_sv_fn = f"evalulated_traj_sm_l512_wana_v3_enum_st_{self.tag}_step_{self.i_step}.npy"
            eval_sv_fn = os.path.join(self.save_dir, eval_sv_fn)
            np.save(eval_sv_fn, evalulated_traj_enum_st)
            print(f"evaluated saved to {eval_sv_fn}")
            if self.cur_policy_loss < self.best_policy_loss:
                self.best_policy_loss = self.cur_policy_loss
                best_eval_sv_fn = f"evalulated_traj_sm_l512_wana_v3_enum_st_best_{self.tag}.npy"
                best_eval_sv_fn = os.path.join(self.save_dir, best_eval_sv_fn)
                np.save(best_eval_sv_fn, evalulated_traj_enum_st)
                print(f"best evaluated enum_st saved to {best_eval_sv_fn} with policy loss: {self.best_policy_loss}")
                best_eval_sv_fn = f"evalulated_traj_sm_l512_wana_v3_best_{self.tag}.npy"
                best_eval_sv_fn = os.path.join(self.save_dir, best_eval_sv_fn)
                np.save(best_eval_sv_fn, evalulated_traj)
                print(f"best evaluated saved to {best_eval_sv_fn} with policy loss: {self.best_policy_loss}")
            
            if (self.use_base_sim_mpc)  or ( (not self.train_wm_only) and (not self.train_visualfeats_policy) ):
                ################# mpc eval #################
                targets_all_frames = self.env.get_tot_targets()
                targets_all_frames = torch.from_numpy(targets_all_frames).float().cuda()
                frame_nums = [_ for _ in range(1, self.env.frame_length + 1)]
                frame_nums = torch.tensor(frame_nums).long().cuda() ### frame_nums ###
                mpc_traj = self.mpc_eval(targets_all_frames, frame_nums)
                mpc_traj_sv_fn = f"evalulated_mpc_traj_sm_l512_wana_v3_{self.tag}_step_{self.i_step}.npy"
                
                mpc_traj_sv_fn = os.path.join(self.save_dir, mpc_traj_sv_fn)
                
                np.save(mpc_traj_sv_fn, mpc_traj)
                print(f"MPC traj saved to {mpc_traj_sv_fn}")
                ################# mpc eval #################
        
        
        # sample trajectories #
        if should_do_subprocess_task: 
            with torch.no_grad():
                ## path ##
                # path : dict = self.runner.trajectory_sampling( math.floor(self.collect_size/max(1, mpi_world_size -1)), self )
                # path : dict = self.runner.trajectory_sampling( 40, self ) # sample the trajectory # 
                path : dict = self.runner.trajectory_sampling( self.nn_traj_samples, self ) #
                self.env.update_val(path['done'], path['rwd'], path['frame_num'])
                
                
                sv_sampled_traj_fn = f"sampled_traj_step_{self.i_step}.npy"
                sv_sampled_traj_fn = os.path.join(self.save_dir, sv_sampled_traj_fn)
                np.save(sv_sampled_traj_fn, path)
                print(f"sampled traj saved to {sv_sampled_traj_fn}")
        else:
            
            path = {}

        tmp = np.zeros_like(self.env.val)
        mpi_comm.Allreduce(self.env.val, tmp) 
        self.env.val = tmp / mpi_world_size 
        self.env.update_p()
        
        print(f"mpi_world_size: {mpi_world_size}")
        
        res = gather_dict_ndarray(path)
        if mpi_rank == 0:
            
            ### parameters for sample ###
            paramter = self.parameters_for_sample() 
            mpi_comm.bcast(paramter, root = 0)
            self.replay_buffer.add_trajectory(res)
            info = {
                'rwd_mean': np.mean(res['rwd']),
                'rwd_std': np.std(res['rwd']),
                'episode_length': len(res['rwd'])/(res['done']!=0).sum()
            }
        else:
            paramter = mpi_comm.bcast(None, root = 0)
            self.load_parameters_for_sample(paramter)    
            info = None
        return info
    
    def run_loop(self):
        """training loop, MPI included
        """
        #self.try_load("save/short/20min/controlVAE/model_3500.data")
        for i in range(0, self.max_iteration):
            # if i ==0:
             ## trajectory samping ##
            self.i_step = i
            info = self.mpi_sync() # communication, collect samples and broadcast policy
            
            
            if self.use_base_sim_mpc:
                continue ### no training ##
            
            if mpi_rank == 0:
                print(f"----------training {i} step--------")
                sys.stdout.flush()
                log = self.train_one_step() 
                log.update(info)       
                self.try_save(i)
                # self.try_log(log, i)

            # if should_do_subprocess_task:
            #     self.try_evaluate(i)
    @property
    def dir_prefix(self):
        return 'Experiment'
    
    def try_save(self, iteration):
        if iteration % self.save_period == 0:
            if not os.path.exists(self.save_dir):
                os.mkdir(self.save_dir)
            
            check_point = {
                'self': self.state_dict(),
                'wm_optim': self.wm_optimizer.state_dict(),
                'policy_optim': self.policy_optimizer.state_dict(),
                'balance': self.env.val
            }
            with bf.BlobFile(bf.join(self.save_dir, f'model_{iteration}.data'), 'wb') as f:
                th.save(check_point, f)
    
    def try_load(self, data_file):
        data = th.load(data_file, map_location=ptu.device)
        self.load_state_dict(data['self'], strict=False)
        self.wm_optimizer.load_state_dict(data['wm_optim'])
        if 'policy_optim' in data:
            self.policy_optimizer.load_state_dict(data['policy_optim'])
        else:
            self.policy_optimizer.load_state_dict(data['vae_optim'])
        if 'balance' in data:
            self.env.val = data['balance']
            self.env.update_p()
        return data
               
    def cal_rwd(self, **obs_info):
        observation = obs_info['observation']
        target = obs_info['target']
        
        
        if self.two_hands:
            error = pose_err_ours_twohands(torch.from_numpy(observation), torch.from_numpy(target), self.weight, dt = self.env.dt)
        else:
            if self.use_ana:
                error = pose_err_ours(observation, target, self.weight, dt = self.env.dt)
            else:
                
                error = pose_err_ours(torch.from_numpy(observation), torch.from_numpy(target), self.weight, dt = self.env.dt)
        error = sum(error).item()
        
        
        return np.exp(-error/20)
    
    def cal_err(self, **obs_info):
        observation = obs_info['observation']
        target = obs_info['target']
        
        if self.two_hands:
            error = pose_err_ours_twohands(torch.from_numpy(observation), torch.from_numpy(target), self.weight, dt = self.env.dt)
        else:
            if self.use_ana:
                error = pose_err_ours(observation, target, self.weight, dt = self.env.dt)
            else:
                error = pose_err_ours(torch.from_numpy(observation), torch.from_numpy(target), self.weight, dt = self.env.dt)
        error = sum(error).item()
        return error
    
    #--------------------------API for encode and decode------------------------------#
    
    def encode(self, normalized_obs, normalized_target, **kargs):
        """encode observation and target into posterior distribution
        Args:
            normalized_obs (Optional[Tensor,np.ndarray]): normalized current observation
            normalized_target (Optional[Tensor, np.ndarray]): normalized current target 

        Returns:
            Tuple(tensor, tensor, tensor): 
                latent coder, mean of prior distribution, mean of posterior distribution 
        """
        
        # if self.use_multi_ed:
            
        
        return self.encoder(normalized_obs, normalized_target)
    
    def decode(self, normalized_obs, latent, **kargs):
        """decode latent code into action space

        Args:
            normalized_obs (tensor): normalized current observation
            latent (tensor): latent code

        Returns:
            tensor: action
        """
        action = self.agent(latent, normalized_obs)        
        return action
    
    def normalize_obs(self, observation):
        if isinstance(observation, np.ndarray):
            observation = torch.from_numpy(observation).float().to(ptu.device) # .cuda()
        if len(observation.shape) == 1: # does not know
            observation = observation[None, ...]
        if self.wnorm:
            return ptu.normalize(observation, self.obs_mean, self.obs_std) #
        else:
            return observation 
    
    def obsinfo2n_obs(self, obs_info):
        if 'n_observation' in obs_info:
            n_observation = obs_info['n_observation']
        else:
            if 'observation' in obs_info:
                observation = obs_info['observation']
            else:
                # observation = state2ob(obs_info['state'])
                observation = obs_info['state'] # # states are observations here #
            n_observation = self.normalize_obs(observation) # 
        return n_observation
    
    
    def act_tracking_visualpolicy(self, **obs_info):
        """
        try to track reference motion
        """
        target = obs_info['target']
        
        observation = obs_info['observation']
        
        n_target = self.env.normalize_observations(target)
        n_observation = self.obsinfo2n_obs(obs_info) ## ## # observation ## three such networks ##
        if 'n_observation' in obs_info:
            n_observation = obs_info['n_observation']
        else:
            n_observation = self.env.normalize_observations(obs_info['observation'])
        
        # if self.use_multi_ed:
        #     # if self.traj_opt:
        #     #     # print(obs_info['frame_num'])
        #     #     action_trans = self.agent_trans(obs_info['frame_num'])
        #     #     action_rot = self.agent_rot(obs_info['frame_num'])
        #     #     action_states = self.agent_states(obs_info['frame_num'])
        #     #     action = torch.cat(
        #     #         [action_trans, action_rot, action_states], dim=-1
        #     #     )
        #     #     info = {}
        #     # else:
            
        latent_code_trans, mu_post_trans, mu_prior_trans = self.encoder_trans(n_observation, n_target, observation, self.world_model)
        action_trans = self.agent_trans(latent_code_trans, n_observation)
        
        latent_code_rot, mu_post_rot, mu_prior_rot = self.encoder_rot(n_observation, n_target, observation, self.world_model)
        action_rot = self.agent_rot(latent_code_rot, n_observation)
        
        latent_code_states, mu_post_states, mu_prior_states = self.encoder_states(n_observation, n_target, observation, self.world_model)
        action_states = self.agent_states(latent_code_states, n_observation)
        
        action = torch.cat(
            [action_trans, action_rot, action_states], dim=-1
        )
        
        # action = n_observation[:, :action.size(-1)] + action
        
        action = n_target[:, :action.size(-1)] + action
        
        action_dummy_state = torch.cat(
            [action, torch.zeros((action.size(0), 7)).cuda()], dim=-1
        )
        action = self.env.denormalize_observations(action_dummy_state)
        action = action[:, :-7]
        
        info = {
            "mu_prior": mu_prior_states, # 
            "mu_post": mu_post_states # 
        }
        
        
        
        if torch.any(torch.isnan(action)):
            print(f"[act tracking] has NaN value in action (aaft dec)")
        
        return action, info
    
    
    ## act tracking ##
    def act_tracking(self, **obs_info):
        
        if self.visualfeats_policy:
            action, info = self.act_tracking_visualpolicy(**obs_info)
            return action, info
        
        """
        try to track reference motion
        """
        target = obs_info['target']
        
        # if torch.any(torch.isnan(target)):
        #     print(f"[act tracking] has NaN value in target") # fine grained manipulations
        
        n_target = self.normalize_obs(target)
        n_observation = self.obsinfo2n_obs(obs_info) 
        
        if self.use_multi_ed:
            if self.traj_opt:
                # print(obs_info['frame_num'])
                action_trans = self.agent_trans(obs_info['frame_num'])
                action_rot = self.agent_rot(obs_info['frame_num'])
                action_states = self.agent_states(obs_info['frame_num'])
                action = torch.cat(
                    [action_trans, action_rot, action_states], dim=-1
                )
                info = {}
            else:
            
                latent_code_trans, mu_post_trans, mu_prior_trans = self.encoder_trans(n_observation, n_target)
                action_trans = self.agent_trans(latent_code_trans, n_observation)
                
                latent_code_rot, mu_post_rot, mu_prior_rot = self.encoder_rot(n_observation, n_target)
                action_rot = self.agent_rot(latent_code_rot, n_observation)
                
                latent_code_states, mu_post_states, mu_prior_states = self.encoder_states(n_observation, n_target)
                action_states = self.agent_states(latent_code_states, n_observation)
                
                action = torch.cat(
                    [action_trans, action_rot, action_states], dim=-1
                )
                
                info = {
                    "mu_prior": mu_prior_states, # 
                    "mu_post": mu_post_states # 
                }
        else:
            if torch.any(torch.isnan(n_target)):
                print(f"[act tracking] has NaN value in n_target")
                
            if torch.any(torch.isnan(n_observation)):
                print(f"[act tracking] has NaN value in n_observation")
            
            ## get the latent code and mu post and mu prior ##
            latent_code, mu_post, mu_prior = self.encode(n_observation, n_target)
            
            if torch.any(torch.isnan(latent_code)):
                print(f"[act tracking] has NaN value in latent_code")
                
            if torch.any(torch.isnan(mu_post)):
                print(f"[act tracking] has NaN value in mu_post")
                
            if torch.any(torch.isnan(mu_prior)):
                print(f"[act tracking] has NaN value in mu_prior")
            
            action = self.decode(n_observation, latent_code)
            
            info = {
                "mu_prior": mu_prior,
                "mu_post": mu_post
            }
            
        
        if torch.any(torch.isnan(action)):
            print(f"[act tracking] has NaN value in action (aaft dec)")
        
        return action, info
    
    
    
    def act_tracking_tmp(self, **obs_info):
        """
        try to track reference motion
        """
        # target = obs_info['target']
        
        # if torch.any(torch.isnan(target)):
        #     print(f"[act tracking] has NaN value in target") # fine grained manipulations
        
        # n_target = self.normalize_obs(target)
        # n_observation = self.obsinfo2n_obs(obs_info) ## ## # observation ## three such networks ##
        
        # print(obs_info['frame_num'])
        action_trans = self.tmp_agent_trans(obs_info['frame_num'])
        action_rot = self.tmp_agent_rot(obs_info['frame_num'])
        action_states = self.tmp_agent_states(obs_info['frame_num'])
        action = torch.cat(
            [action_trans, action_rot, action_states], dim=-1
        )
        info = {}
        
        # if self.use_multi_ed:
        #     if self.traj_opt:
        #         # print(obs_info['frame_num'])
        #         action_trans = self.agent_trans(obs_info['frame_num'])
        #         action_rot = self.agent_rot(obs_info['frame_num'])
        #         action_states = self.agent_states(obs_info['frame_num'])
        #         action = torch.cat(
        #             [action_trans, action_rot, action_states], dim=-1
        #         )
        #         info = {}
        #     else:
            
        #         latent_code_trans, mu_post_trans, mu_prior_trans = self.encoder_trans(n_observation, n_target)
        #         action_trans = self.agent_trans(latent_code_trans, n_observation)
                
        #         latent_code_rot, mu_post_rot, mu_prior_rot = self.encoder_rot(n_observation, n_target)
        #         action_rot = self.agent_rot(latent_code_rot, n_observation)
                
        #         latent_code_states, mu_post_states, mu_prior_states = self.encoder_states(n_observation, n_target)
        #         action_states = self.agent_states(latent_code_states, n_observation)
                
        #         action = torch.cat(
        #             [action_trans, action_rot, action_states], dim=-1
        #         )
                
        #         info = {
        #             "mu_prior": mu_prior_states, # 
        #             "mu_post": mu_post_states # 
        #         }
        # else:
        #     if torch.any(torch.isnan(n_target)):
        #         print(f"[act tracking] has NaN value in n_target")
                
        #     if torch.any(torch.isnan(n_observation)):
        #         print(f"[act tracking] has NaN value in n_observation")
            
        #     ## get the latent code and mu post and mu prior ##
        #     latent_code, mu_post, mu_prior = self.encode(n_observation, n_target)
            
        #     if torch.any(torch.isnan(latent_code)):
        #         print(f"[act tracking] has NaN value in latent_code")
                
        #     if torch.any(torch.isnan(mu_post)):
        #         print(f"[act tracking] has NaN value in mu_post")
                
        #     if torch.any(torch.isnan(mu_prior)):
        #         print(f"[act tracking] has NaN value in mu_prior")
            
        #     action = self.decode(n_observation, latent_code)
            
        #     info = {
        #         "mu_prior": mu_prior,
        #         "mu_post": mu_post
        #     }
            
        
        if torch.any(torch.isnan(action)):
            print(f"[act tracking] has NaN value in action (aaft dec)")
        
        return action, info
    
    
    
    #----------------------------------API imitate PPO--------------------------------#
    def act_determinastic(self, obs_info):
        
        action, _ = self.act_tracking(**obs_info)
        
        
        return action
    
    def act_distribution(self, obs_info):
        """
        Add noise to the output action
        """
        ## TODO: add diferent kines of noise to differnet types of information ## # type of information #
        action = self.act_determinastic(obs_info)
        if self.traj_opt:
            dist_sigma = torch.cat(
                [torch.ones_like(action)[..., :3] * self.trans_action_sigma, torch.ones_like(action)[..., 3:7] * self.rot_action_sigma, torch.ones_like(action)[..., 7:] * self.finger_state_action_sigma], dim=-1
            ) # world model? #
            action_distribution = D.Independent(D.Normal(action, dist_sigma), -1)
            
            
            
        else:
            action_distribution = D.Independent(D.Normal(action, self.action_sigma), -1)
        return action_distribution
    
    ## action distribution ##
    #--------------------------------------Utils--------------------------------------#
    @staticmethod
    def merge_dict(dict_list: List[dict], prefix: List[str]):
        """Merge dict with prefix, used in merge logs from different model

        Args:
            dict_list (List[dict]): different logs
            prefix (List[str]): prefix you hope to add before keys
        """
        res = {}
        for dic, prefix in zip(dict_list, prefix):
            for key, value in dic.items():
                res[prefix+'_'+key] = value
        return res
    
    
    def train_policy_visualfeats(self, states, targets, frame_nums): # rollout trajectories ## ## ## 
        print(f"Training visualfeats policy")
        rollout_length = states.shape[1]  ## rollout length ## #  # ## train policy ##
        # mano_trans_loss, mano_rot_loss, mano_states_loss, obj_rot_loss, obj_trans_loss
        loss_name = ['mano_trans', 'mano_rot', 'mano_states', 'obj_rot', 'obj_trans', 'act_loss']
        loss_num = len(loss_name)
        loss = list( ([] for _ in range(loss_num)) ) #get losses #
        states = states.transpose(0,1).contiguous().to(ptu.device)
        targets = targets.transpose(0,1).contiguous().to(ptu.device) ##targets tra
        
        frame_nums = frame_nums.transpose(0, 1).contiguous().to(ptu.device)
        cur_state = states[0]
        
        cur_frame_num = frame_nums[0, 0].item()
        
        tot_states = []
        tot_states_after_ana = []
        
        # ## reset the analytical sim ## #
        self.world_model.reset(cur_state[0], int(cur_frame_num) - 1)
        
        if self.use_ana:
            cur_frame_num = frame_nums[0, 0].item()
            self.env.counter = int(cur_frame_num)
            self.env.reset_state() # use the crrents state to reset the env? other than using the timestep to reset this? #
        # cur_observation = state2ob(cur_state)
        cur_observation = cur_state
        # n_observation = self.normalize_obs(cur_observation)
        
        n_observation = self.env.normalize_observations(cur_observation)
        
        print(f"frame_nums: {frame_nums[:, 0]}")
        
        for i in range(rollout_length): ## ## rollout_length ## ##
            target = targets[i] # target ## ##
            frame_num = frame_nums[i]
            ## check nan ##
            if torch.any(torch.isnan(target)):
                print(f"[tarin policy] frame {i} has NaN value in target") # ##  
            if torch.any(torch.isnan(n_observation)): # 
                print(f"[tarin policy] frame {i} has NaN value in n_observation")
            if torch.any(torch.isnan(cur_state)): # 
                print(f"[tarin policy] frame {i} has NaN value in cur_state")
            
            ## 
            ## then the act tracking ##
            action, info = self.act_tracking(n_observation = n_observation, target = target, frame_num=frame_num, observation=cur_observation)
            
            if torch.any(torch.isnan(action)):
                print(f"[tarin policy] frame {i} has NaN value in action")
            
            # action = action + torch.randn_like(action)*0.05 # # 
            # action = action + torch.randn_like(action)*0.005 # # with ana # # eval one traj -> states with actions -> the next states #
            # action = # 
            action = action + torch.randn_like(action)*0.00 # # randnlike # policy optimization? #
            
            # print(f"action: {action}")
            if torch.any(torch.isnan(action)):
                
                print(f"[tarin policy] frame {i} has NaN value in action after rand")
                
            # if self.use_ana:
            #     # cur_state and action
            #     observation, reward, done, info = self.env.step_core(action) ## 
            #     cur_state = observation['state'].unsqueeze(0)
            #     cur_observation = cur_state
            #     if len(action.size()) == 1:
            #         action = action.unsqueeze(0)
            # else:
            cur_state = self.world_model(cur_state, action, n_observation = n_observation)
            # cur_observation = state2ob(cur_state) ## world model ## 
            cur_observation = cur_state #
                
            ## tot_states, tot_states_after_ana # fit a aworld model ## 
            tot_states.append(cur_observation.detach().cpu().numpy())
            if self.world_model.wana and not self.world_model.pred_mano_obj_states_woana:
                tot_states_after_ana.append(self.world_model.observation_ana.detach().cpu().numpy())
                
            
            # n_observation = self.normalize_obs(cur_observation)
            
            n_observation = self.env.normalize_observations(cur_observation)
            
            # print(f"cur_observation: {cur_observation.shape}, target: {target.shape}")

            ## TODO: pose err> # pose errors # 
            if self.two_hands:
                loss_tmp = pose_err_ours_twohands(cur_observation, target, self.weight, dt = self.env.dt)
            else:
                loss_tmp = pose_err_ours(cur_observation, target, self.weight, dt = self.env.dt, actor=self)
            
            # loss here contains 'pos', 'rot', 'vel', 'avel', 'height', 'up_dir'
            for j, value in enumerate(loss_tmp):
                loss[j].append(value)        
                
            ### no act loss? ##
            
            # acs_loss = self.weight['l2'] * torch.mean(torch.sum(action**2,dim = -1)) \
            #     + self.weight['l1'] * torch.mean(torch.norm(action, p=1, dim=-1))
            
            act_loss_trans = self.weight['l2'] * 10 *  torch.mean(torch.sum(action[:, :3]**2,dim = -1))  \
                + self.weight['l1'] * 10 * torch.mean(torch.norm(action[:, :3], p=1, dim=-1))
            act_loss_rot = self.weight['l2'] * 10 *  torch.mean(torch.sum(action[:, 3:6]**2,dim = -1))  \
                + self.weight['l1'] * 10 * torch.mean(torch.norm(action[:, 3:6], p=1, dim=-1))
            act_loss_states = self.weight['l2'] *  torch.mean(torch.sum(action[:, 6:]**2,dim = -1))  \
                + self.weight['l1'] * torch.mean(torch.norm(action[:, 6:], p=1, dim=-1))
            acs_loss = act_loss_trans + act_loss_rot + act_loss_states
            # kl_loss = self.encoder.kl_loss(**info)
            
            # # kl_loss = torch.mean( torch.sum(kl_loss, dim = -1))
            loss[-1].append(acs_loss)
        
        ## sumof the loss value ###
        # discount_factor = 0.95 # 
        discount_factor = 1.0 # 
        # discount_factor = 0.90
        # loss_value = [ sum( (0.95**i)*l[i] for i in range(rollout_length) )/rollout_length for l in loss] # loss in ##
        loss_value = [ sum( (discount_factor**i)*l[i] for i in range(rollout_length) )/rollout_length for l in loss]
        print(f"[train policy] loss: {loss_value}")
        if self.traj_opt:
            loss = sum(loss_value[:-1]) # discount  # no action reg losses here ##
        else:   
            loss = sum(loss_value[:]) # discount 
        
        tot_act_loss = loss_value[-1]

        if self.wandb:
            wandb.log({"policy_loss": loss})
            wandb.log({"acs_loss": tot_act_loss})
            
        self.cur_policy_loss =loss

        self.policy_optimizer.zero_grad() # zero grad #
        loss.backward(retain_graph=True)
        # torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), 1, error_if_nonfinite=True)
        # torch.nn.utils.clip_grad_norm_(self.agent.parameters(), 1, error_if_nonfinite= True)
        if self.use_multi_ed:
            torch.nn.utils.clip_grad_norm_(self.encoder_trans.parameters(), 1, error_if_nonfinite=False)
            torch.nn.utils.clip_grad_norm_(self.agent_trans.parameters(), 1, error_if_nonfinite= False)
            torch.nn.utils.clip_grad_norm_(self.encoder_rot.parameters(), 1, error_if_nonfinite=False)
            torch.nn.utils.clip_grad_norm_(self.agent_rot.parameters(), 1, error_if_nonfinite= False)
            torch.nn.utils.clip_grad_norm_(self.encoder_states.parameters(), 1, error_if_nonfinite=False)
            torch.nn.utils.clip_grad_norm_(self.agent_states.parameters(), 1, error_if_nonfinite= False)
        else:
            torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), 1, error_if_nonfinite=False)
            torch.nn.utils.clip_grad_norm_(self.agent.parameters(), 1, error_if_nonfinite= False)
        self.policy_optimizer.step() #
        self.beta_scheduler.step() #
        res = {loss_name[i]: loss_value[i] for i in range(loss_num)}
        res['beta'] = self.beta_scheduler.value
        res['loss'] = loss
        
        # ## tot_states, tot_states_after_ana # 
        tot_states = np.stack(tot_states, axis = 0)
        try:
            tot_states_after_ana = np.stack(tot_states_after_ana, axis=0) ## rollout_length x nn_batch x obs_dim ##
            res['tot_states_after_ana'] = tot_states_after_ana # .transpose(1, 0)
        except:
            pass
        res['tot_states'] = tot_states # .transpose(1, 0)
        
        res['targets'] = targets.detach().cpu().numpy()
        res['states'] = states.detach().cpu().numpy()
        
        return res
    
    ## extract visual features ##
    ## use the visual feature and the current state to predict the action -> using the current state as the bias term ##
    ## get the next step's action ##
    def train_policy(self, states, targets, frame_nums): # rollout trajectories ## ## ## 
        
        if self.train_visualfeats_policy:
            res = self.train_policy_visualfeats(states, targets, frame_nums)
            return res
        
        rollout_length = states.shape[1]  ## rollout length ## #  # ## train policy ##
        # mano_trans_loss, mano_rot_loss, mano_states_loss, obj_rot_loss, obj_trans_loss
        loss_name = ['mano_trans', 'mano_rot', 'mano_states', 'obj_rot', 'obj_trans', 'act_loss']
        loss_num = len(loss_name)
        loss = list( ([] for _ in range(loss_num)) ) #get losses #
        states = states.transpose(0,1).contiguous().to(ptu.device)
        targets = targets.transpose(0,1).contiguous().to(ptu.device) ##targets tra
        
        frame_nums = frame_nums.transpose(0, 1).contiguous().to(ptu.device)
        cur_state = states[0]
        
        cur_frame_num = frame_nums[0, 0].item()
        
        tot_states = []
        tot_states_after_ana = []
        
        # ## reset the analytical sim ## #
        self.world_model.reset(cur_state[0], int(cur_frame_num) - 1)
        
        if self.use_ana:
            cur_frame_num = frame_nums[0, 0].item()
            self.env.counter = int(cur_frame_num)
            self.env.reset_state() # use the crrents state to reset the env? other than using the timestep to reset this? #
        # cur_observation = state2ob(cur_state)
        cur_observation = cur_state
        n_observation = self.normalize_obs(cur_observation)
        
        print(f"frame_nums: {frame_nums[:, 0]}")
        
        for i in range(rollout_length): ## ## rollout_length ## ##
            target = targets[i] # target ## ##
            frame_num = frame_nums[i]
            ## check nan ##
            if torch.any(torch.isnan(target)):
                print(f"[tarin policy] frame {i} has NaN value in target") # ##  
            if torch.any(torch.isnan(n_observation)): # 
                print(f"[tarin policy] frame {i} has NaN value in n_observation")
            if torch.any(torch.isnan(cur_state)): # 
                print(f"[tarin policy] frame {i} has NaN value in cur_state")
            
            ## 
            ## then the act tracking ##
            action, info = self.act_tracking(n_observation = n_observation, target = target, frame_num=frame_num)
            
            if torch.any(torch.isnan(action)):
                print(f"[tarin policy] frame {i} has NaN value in action")
            
            # action = action + torch.randn_like(action)*0.05 # # 
            # action = action + torch.randn_like(action)*0.005 # # with ana # # eval one traj -> states with actions -> the next states #
            # action = # 
            action = action + torch.randn_like(action)*0.00 # # randnlike # policy optimization? #
            
            # print(f"action: {action}")
            if torch.any(torch.isnan(action)):
                
                print(f"[tarin policy] frame {i} has NaN value in action after rand")
                
            if self.use_ana:
                # cur_state and action
                observation, reward, done, info = self.env.step_core(action) ## 
                cur_state = observation['state'].unsqueeze(0)
                cur_observation = cur_state
                if len(action.size()) == 1:
                    action = action.unsqueeze(0)
            else:
                cur_state = self.world_model(cur_state, action, n_observation = n_observation)
                # cur_observation = state2ob(cur_state) ## world model ## 
                cur_observation = cur_state #
                
            ## tot_states, tot_states_after_ana # fit a aworld model ## 
            tot_states.append(cur_observation.detach().cpu().numpy())
            if self.world_model.wana and not self.world_model.pred_mano_obj_states_woana:
                tot_states_after_ana.append(self.world_model.observation_ana.detach().cpu().numpy())
                
            
            n_observation = self.normalize_obs(cur_observation)
            
            # print(f"cur_observation: {cur_observation.shape}, target: {target.shape}")

            ## TODO: pose err> # pose errors # 
            if self.two_hands:
                loss_tmp = pose_err_ours_twohands(cur_observation, target, self.weight, dt = self.env.dt)
            else:
                loss_tmp = pose_err_ours(cur_observation, target, self.weight, dt = self.env.dt, actor=self)
            
            # loss here contains 'pos', 'rot', 'vel', 'avel', 'height', 'up_dir'
            for j, value in enumerate(loss_tmp):
                loss[j].append(value)        
                
            ### no act loss? ##
            
            # acs_loss = self.weight['l2'] * torch.mean(torch.sum(action**2,dim = -1)) \
            #     + self.weight['l1'] * torch.mean(torch.norm(action, p=1, dim=-1))
            
            act_loss_trans = self.weight['l2'] * 10 *  torch.mean(torch.sum(action[:, :3]**2,dim = -1))  \
                + self.weight['l1'] * 10 * torch.mean(torch.norm(action[:, :3], p=1, dim=-1))
            act_loss_rot = self.weight['l2'] * 10 *  torch.mean(torch.sum(action[:, 3:6]**2,dim = -1))  \
                + self.weight['l1'] * 10 * torch.mean(torch.norm(action[:, 3:6], p=1, dim=-1))
            act_loss_states = self.weight['l2'] *  torch.mean(torch.sum(action[:, 6:]**2,dim = -1))  \
                + self.weight['l1'] * torch.mean(torch.norm(action[:, 6:], p=1, dim=-1))
            acs_loss = act_loss_trans + act_loss_rot + act_loss_states
            # kl_loss = self.encoder.kl_loss(**info)
            
            # # kl_loss = torch.mean( torch.sum(kl_loss, dim = -1))
            loss[-1].append(acs_loss)
        
        ## sumof the loss value ###
        # discount_factor = 0.95 # 
        discount_factor = 1.0
        # discount_factor = 0.90
        # loss_value = [ sum( (0.95**i)*l[i] for i in range(rollout_length) )/rollout_length for l in loss] # loss in ##
        loss_value = [ sum( (discount_factor**i)*l[i] for i in range(rollout_length) )/rollout_length for l in loss]
        print(f"[train policy] loss: {loss_value}")
        if self.traj_opt:
            loss = sum(loss_value[:-1]) # discount  # no action reg losses here ##
        else:   
            loss = sum(loss_value[:]) # discount 
        
        tot_act_loss = loss_value[-1]

        if self.wandb:
            wandb.log({"policy_loss": loss})
            wandb.log({"acs_loss": tot_act_loss})
            
        self.cur_policy_loss =loss

        self.policy_optimizer.zero_grad() # zero grad #
        loss.backward(retain_graph=True)
        # torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), 1, error_if_nonfinite=True)
        # torch.nn.utils.clip_grad_norm_(self.agent.parameters(), 1, error_if_nonfinite= True)
        if self.use_multi_ed:
            torch.nn.utils.clip_grad_norm_(self.encoder_trans.parameters(), 1, error_if_nonfinite=False)
            torch.nn.utils.clip_grad_norm_(self.agent_trans.parameters(), 1, error_if_nonfinite= False)
            torch.nn.utils.clip_grad_norm_(self.encoder_rot.parameters(), 1, error_if_nonfinite=False)
            torch.nn.utils.clip_grad_norm_(self.agent_rot.parameters(), 1, error_if_nonfinite= False)
            torch.nn.utils.clip_grad_norm_(self.encoder_states.parameters(), 1, error_if_nonfinite=False)
            torch.nn.utils.clip_grad_norm_(self.agent_states.parameters(), 1, error_if_nonfinite= False)
        else:
            torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), 1, error_if_nonfinite=False)
            torch.nn.utils.clip_grad_norm_(self.agent.parameters(), 1, error_if_nonfinite= False)
        self.policy_optimizer.step() #
        self.beta_scheduler.step() #
        res = {loss_name[i]: loss_value[i] for i in range(loss_num)}
        res['beta'] = self.beta_scheduler.value
        res['loss'] = loss
        
        # ## tot_states, tot_states_after_ana # 
        tot_states = np.stack(tot_states, axis = 0)
        try:
            tot_states_after_ana = np.stack(tot_states_after_ana, axis=0) ## rollout_length x nn_batch x obs_dim ##
            res['tot_states_after_ana'] = tot_states_after_ana # .transpose(1, 0)
        except:
            pass
        res['tot_states'] = tot_states # .transpose(1, 0)
        
        res['targets'] = targets.detach().cpu().numpy()
        res['states'] = states.detach().cpu().numpy()
        
        return res
    
    #  agent_trans.frame_data_embedding_layer.weight.data #
    def mpc_eval(self, targets, frame_nums): # rollout trajectories ## ## ## 
        # rollout_length = states.shape[1]  ## rollout length ## #  # ## train policy ##
        # mano_trans_loss, mano_rot_loss, mano_states_loss, obj_rot_loss, obj_trans_loss
        # loss_name = ['mano_trans', 'mano_rot', 'mano_states', 'obj_rot', 'obj_trans', 'act_loss']
        # loss_num = len(loss_name) # loss name #
        # loss = list( ([] for _ in range(loss_num)) ) #get losses #
        
        
        # st_agent_trans = self.agent_trans.frame_data_embedding_layer.weight.data[0].clone()
        # st_agent_rot = self.agent_rot.frame_data_embedding_layer.weight.data[0].clone()
        # st_agent_states = self.agenet_states.frame_data_embedding_layer.weight.data[0].clone()
        
        self.tmp_agent_trans = GatingMixedDecoderV2(
            # latent_size= kargs['latent_size'],
            condition_size=self.observation_size,
            output_size=3,
            nn_frames=self.mano_glb_trans.size(0),
            nn_dim=self.mano_glb_trans.size(1),
            frame_data=self.agent_trans.frame_data_embedding_layer.weight.data.clone(),
            **self.kargs
        ).to(ptu.device)
        
        self.tmp_agent_rot = GatingMixedDecoderV2(
            # latent_size= kargs['latent_size'],
            condition_size=self.observation_size,
            output_size=3,
            nn_frames=self.mano_glb_rot.size(0),
            nn_dim=self.mano_glb_rot.size(1),
            frame_data=self.agent_rot.frame_data_embedding_layer.weight.data.clone(),
            **self.kargs
        ).to(ptu.device)
        
        self.tmp_agent_states = GatingMixedDecoderV2(
            # latent_size= kargs['latent_size'],
            condition_size=self.observation_size,
            output_size=3,
            nn_frames=self.mano_states.size(0),
            nn_dim=self.mano_states.size(1),
            frame_data=self.agent_states.frame_data_embedding_layer.weight.data.clone(),
            **self.kargs
        ).to(ptu.device)
        
        self.tmp_policy_optimizer = RAdam(list(self.tmp_agent_trans.parameters()) + list(self.tmp_agent_rot.parameters()) + list(self.tmp_agent_states.parameters()), self.kargs['policy_lr'])
        
        
        # cur_trans_agent_weights, cur_rot_agent_weights, cur_states_agent_weights
        # cur_trans_agent_weights = self.agent_trans.frame_data_embedding_layer.weight.data.clone()
        # cur_rot_agent_weights = self.agent_rot.frame_data_embedding_layer.weight.data.clone()
        # cur_states_agent_weights = self.agent_states.frame_data_embedding_layer.weight.data.clone()
        
        # st_agent_staes; st_agent_rot; st_agent_trans #
        tot_frame_length = self.env.frame_length ## env's frmae length #
        
        ## frmae length ##
        observation, info = self.env.reset(frame=0)
        
        cur_state = observation['state'] # .unsqueeze(0)
        cur_state_th = torch.from_numpy(cur_state).float().to(ptu.device) # 
        
        states = []
        actions = []
        res_targets = []
        rwds = []
        dones = []
        
        ### from cur_ ###
        
        for i_fr in range(tot_frame_length):
            print(f"i_fr: {i_fr}")
            cur_state_th_ori = cur_state_th.clone()
            
            # tot_states = []
            
            for i_sub_iter in range(3):
                loss_name = ['mano_trans', 'mano_rot', 'mano_states', 'obj_rot', 'obj_trans', 'act_loss']
                loss_num = len(loss_name) # loss name #
                loss = list( ([] for _ in range(loss_num)) ) #get losses #
                
                cur_state_th = cur_state_th_ori.clone()
                # ## reset the analytical sim ## #
                self.world_model.reset(cur_state_th, int(frame_nums[i_fr].item()) - 1)
                
                

                for i_nex_step in range(i_fr, min(tot_frame_length, i_fr+10)):
                    # print(f"i_nex_step: {i_nex_step}")
                    target = targets[i_nex_step]
                    frame_num = frame_nums[i_nex_step]
                    # cur_state_th ## -> target state ## and the frame_num ##
                    action, info = self.act_tracking_tmp(n_observation = cur_state_th, target = target, frame_num=frame_num)

                    action = action + torch.randn_like(action)*0.00 # # randnlike # policy optimization? #

                    ### wm and wm observations ###
                    cur_state_th_pred = self.world_model(cur_state_th.unsqueeze(0), action, n_observation = cur_state_th.unsqueeze(0))
                    # cur_observation = state2ob(cur_state) ## world model ## 
                    cur_observation = cur_state_th_pred # cur state th ## state th ## state th ## observation ##
                    
                    ## cur observation ##
                    ## tot_states, tot_states_after_ana # fit a aworld model ## 
                    # tot_states.append(cur_observation.detach().cpu().numpy())
                    ## TODO: pose err> # pose errors # cur_state ## get the error #
                    if self.two_hands:
                        loss_tmp = pose_err_ours_twohands(cur_state_th_pred, target.unsqueeze(0), self.weight, dt = self.env.dt)
                    else:
                        loss_tmp = pose_err_ours(cur_state_th_pred, target.unsqueeze(0), self.weight, dt = self.env.dt, actor=self)
                
                    # loss here contains 'pos', 'rot', 'vel', 'avel', 'height', 'up_dir'
                    for j, value in enumerate(loss_tmp):
                        loss[j].append(value)        

                    loss[-1].append(torch.zeros((1,), dtype=torch.float32).cuda().item()) ## cuda and the item ##
                    
                    cur_state_th = cur_state_th_pred.squeeze(0)
                
                rollout_length = min(tot_frame_length, i_fr+10) - i_fr 
                discount_factor = 1.0
                loss_value = [ sum( (discount_factor**i)*l[i] for i in range(rollout_length) )/rollout_length for l in loss]
                print(f"[train policy] loss: {loss_value}")
                # if self.traj_opt:
                
                loss = sum(loss_value[:-1])
                
                # loss = sum(loss_value[3:5])
                
                self.tmp_policy_optimizer.zero_grad() # zero grad #
                loss.backward(retain_graph=True)
                # torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), 1, error_if_nonfinite=True)
                # torch.nn.utils.clip_grad_norm_(self.agent.parameters(), 1, error_if_nonfinite= True)
                # if self.use_multi_ed:
                # torch.nn.utils.clip_grad_norm_(self.encoder_trans.parameters(), 1, error_if_nonfinite=False)
                torch.nn.utils.clip_grad_norm_(self.tmp_agent_trans.parameters(), 1, error_if_nonfinite= False)
                # torch.nn.utils.clip_grad_norm_(self.encoder_rot.parameters(), 1, error_if_nonfinite=False)
                torch.nn.utils.clip_grad_norm_(self.tmp_agent_rot.parameters(), 1, error_if_nonfinite= False)
                # torch.nn.utils.clip_grad_norm_(self.encoder_states.parameters(), 1, error_if_nonfinite=False)
                torch.nn.utils.clip_grad_norm_(self.tmp_agent_states.parameters(), 1, error_if_nonfinite= False)
                
                self.tmp_policy_optimizer.step() # policy optimizer ##
                # self.beta_scheduler.step() # policy optimizer #
            
            ## frame_nums --- from 1 to xxxx ##
            ## targets --- target frame from 1 to xxxx ##
            pred_act, _ = self.act_tracking_tmp(n_observation = cur_state_th_ori, target = targets[i_fr], frame_num=frame_nums[i_fr])
            pred_act_npy = ptu.to_numpy(pred_act).flatten() 
            new_observation, cur_step_pd_control, rwd, done, info   = self.env.step_core_new_wcontrol(pred_act_npy)

            states.append(observation['state']) ## state ##
            actions.append(pred_act_npy) ## action ##
            res_targets.append(observation['target']) ## target ##
            
            cur_state = new_observation['state']
            cur_state_th = torch.from_numpy(cur_state).float().cuda() ### get ne sstate ##
        
            rwd = self.cal_rwd(observation = new_observation['observation'], target = observation['target']) ## target observation; 
            rwds.append(rwd)
            dones.append(done)
            # frame_nums.append(observation['frame_num'])
            observation = new_observation

        self.tmp_policy_optimizer.zero_grad() #
        # # cur_trans_agent_weights, cur_rot_agent_weights, cur_states_agent_weights
        # self.agent_trans.frame_data_embedding_layer.weight.data[:, :] = cur_trans_agent_weights
        # self.agent_rot.frame_data_embedding_layer.weight.data[:, :] = cur_rot_agent_weights
        # self.agent_states.frame_data_embedding_layer.weight.data[:, :] = cur_states_agent_weights
        
        # self.agent_trans = GatingMixedDecoderV2(
        #     # latent_size= kargs['latent_size'],
        #     condition_size=self.observation_size,
        #     output_size=3,
        #     nn_frames=self.mano_glb_trans.size(0),
        #     nn_dim=self.mano_glb_trans.size(1),
        #     frame_data=cur_trans_agent_weights,
        #     **self.kargs
        # ).to(ptu.device)
        
        # self.agent_rot = GatingMixedDecoderV2(
        #     # latent_size= kargs['latent_size'],
        #     condition_size=self.observation_size,
        #     output_size=3,
        #     nn_frames=self.mano_glb_trans.size(0),
        #     nn_dim=self.mano_glb_trans.size(1),
        #     frame_data=cur_rot_agent_weights,
        #     **self.kargs
        # ).to(ptu.device)
        
        # self.agent_states = GatingMixedDecoderV2(
        #     # latent_size= kargs['latent_size'],
        #     condition_size=self.observation_size,
        #     output_size=3,
        #     nn_frames=self.mano_glb_trans.size(0),
        #     nn_dim=self.mano_glb_trans.size(1),
        #     frame_data=cur_states_agent_weights,
        #     **self.kargs
        # ).to(ptu.device)
        
        

        sampled_dict = { ## can get the initial state and use that to reset the env ##
            'state': states,
            'action': actions,
            'target': res_targets,
            'done': dones,
            'rwd': rwds,
            'frame_num': frame_nums.detach().cpu().numpy(),
            # 'robot_controls': robot_controls, ## 
        }
        
        return sampled_dict
    
    
    def eval_world_model(self, ):
        
        self.world_model.reset(None, 0)
        
        dummy_states = None
        
        for i in range(40):
            if dummy_states is None:
                dummy_states = torch.zeros((1, 60), dtype=torch.float32).cuda()
            action_trans = self.agent_trans(i + 1)
            action_rot = self.agent_rot(i + 1)
            action_states = self.agent_states(i + 1)
            
            action = torch.cat( [action_trans, action_rot, action_states], dim=-1 )
            
            dummy_states = self.world_model(dummy_states, action)
            
        cur_rollout_transformed_obj_pts_dict = {
            ts: self.world_model.ts_to_transformed_obj_pts[ts].detach().cpu().numpy() for ts in self.world_model.ts_to_transformed_obj_pts
        }
        # transformed active mesh #
        cur_rollout_transformed_hand_pts_dict = {
            ts: self.world_model.timestep_to_active_mesh[ts].detach().cpu().numpy() for ts in self.world_model.timestep_to_active_mesh
        }
        cur_rollout_sv_info = {
            'transformed_obj_pts': cur_rollout_transformed_obj_pts_dict,
            'transformed_hand_pts': cur_rollout_transformed_hand_pts_dict, ### cotnrol info 
            # 'ts_to_gt_transformed_obj_pts': ts_to_gt_transformed_obj_pts,
            'st_fr_num': 0,
            'ed_fr_num': 44
        }
        
        res = {}
        res['cur_rollout_sv_info'] = cur_rollout_sv_info
        return res
    
    
    
    def train_world_model(self, states, actions, frame_nums):
        rollout_length = states.shape[1] - 1
        # loss_name = ['pos', 'rot', 'vel', 'avel']
        loss_name = ['mano_trans', 'mano_rot', 'mano_states', 'obj_rot', 'obj_trans']
        loss_num = len(loss_name) ## loss ##
        loss = list( ([] for _ in range(loss_num)) )
        states = states.transpose(0,1).contiguous().to(ptu.device) ## wm states ##
        actions = actions.transpose(0,1).contiguous().to(ptu.device) # ## wm actions ##
        cur_state = states[0]
        
        frame_nums = frame_nums.transpose(0, 1).contiguous().to(ptu.device)
        cur_frame_num = frame_nums[0, 0].item()
        
        # statte at frame 0 with action to frmae 1 to target at frame 1
        # set to cur_frame_num - 1 init state at frame 0  ##  # 
        self.world_model.reset(cur_state[0], int(cur_frame_num) - 1) # ## reset ##
        # reset the analytical sim ##
        ts_to_gt_transformed_obj_pts = {}
        
        gt_nex_state_obj_rot = cur_state[0, -4:]
        gt_nex_state_obj_rot = gt_nex_state_obj_rot[[3, 0, 1, 2]]
        gt_nex_state_obj_trans = cur_state[0, -7:-4]
        transformed_obj_pts = self.world_model.forward_kinematics_obj(gt_nex_state_obj_rot, gt_nex_state_obj_trans).detach()
        ts_to_gt_transformed_obj_pts[self.world_model.cur_ts] = transformed_obj_pts.detach().cpu().numpy()
        
        
        for i in range(rollout_length): # 
            
            next_state = states[i+1] # 
            # # 
            pred_next_state = self.world_model(cur_state, actions[i]) # actions ## pred delta and integrate for the nex tstate # 
            # print("[train wm]", pred_next_state[0, :6], actions[i][0, :7], next_state[0, :6]) # 
            loss_tmp = self.world_model.loss(pred_next_state, next_state) # 
            cur_state = pred_next_state # 
            for j in range(loss_num): # 
                loss[j].append(loss_tmp[j]) # 

            # train model ##
            gt_nex_state_obj_rot = next_state[0, -4:] ## x y z w ##
            gt_nex_state_obj_rot = gt_nex_state_obj_rot[[3, 0, 1, 2]]
            gt_nex_state_obj_trans = next_state[0, -7:-4]
            transformed_obj_pts = self.world_model.forward_kinematics_obj(gt_nex_state_obj_rot, gt_nex_state_obj_trans).detach()
            ts_to_gt_transformed_obj_pts[self.world_model.cur_ts] = transformed_obj_pts.detach().cpu().numpy()
                
        st_fr_num = int(frame_nums[0, 0].detach().cpu().item()) - 1 ## start frame number 
        end_frame_num = int(frame_nums[rollout_length - 1, 0].detach().cpu().item()) ## end frame number 
        # get the transformed pcs from the wm ##
        # ts_to_transformed_obj_pts, timestep_to_active_mesh #
        # cur_rollout_transformed_obj_pts_dict = {
        #     ts: self.world_model.ts_to_transformed_obj_pts[ts].detach().cpu().numpy() for ts in range(st_fr_num, end_frame_num + 1)
        # }
        # # transformed active mesh #
        # cur_rollout_transformed_hand_pts_dict = {
        #     ts: self.world_model.timestep_to_active_mesh[ts].detach().cpu().numpy() for ts in range(st_fr_num, end_frame_num + 1)
        # }
        
        # cur_rollout_contact_act_pts = {
        #     ts: self.world_model.ana_sim.ts_to_contact_act_pts[ts] for ts in self.world_model.ana_sim.ts_to_contact_act_pts
        # }
        # cur_rollout_contact_passive_pts = {
        #     ts: self.world_model.ana_sim.ts_to_contact_passive_pts[ts] for ts in self.world_model.ana_sim.ts_to_contact_passive_pts
        # }
        
        # cur_rollout_sv_info = {
        #     'transformed_obj_pts': cur_rollout_transformed_obj_pts_dict,
        #     'transformed_hand_pts': cur_rollout_transformed_hand_pts_dict, ### cotnrol_info ###
        #     'ts_to_gt_transformed_obj_pts': ts_to_gt_transformed_obj_pts, ### ###
        #     'ts_to_contact_act_pts': cur_rollout_contact_act_pts,
        #     'ts_to_contact_passive_pts': cur_rollout_contact_passive_pts,
        #     'st_fr_num': st_fr_num,
        #     'ed_fr_num': end_frame_num
        # }
        
        loss_value = [sum(i) for i in loss]
        
        print(f"[train wm] {loss_value}")
        loss = sum(loss_value)
        
        if self.wandb:
            wandb.log({"world_model_loss": loss})
        
        self.wm_optimizer.zero_grad()
        try:
            loss.backward(retain_graph=True)
            torch.nn.utils.clip_grad_norm_(self.world_model.parameters(), 1, error_if_nonfinite=True) # clip grad norm #
            self.wm_optimizer.step()
        except:
            pass
        res= { loss_name[i]: loss_value[i] for i in range(loss_num) }
        res['loss'] = loss
        # res['cur_rollout_sv_info'] = cur_rollout_sv_info
        return res
    
    ## 3
    def calc_trajectory(self, targets, **model_kwargs):
        states = model_kwargs['states'] # ## 
        saver = model_kwargs['saver'] # 
        env = model_kwargs['env'] # 
        
        cur_state = states[0] # 
        # cur_observation = state2ob(cur_state)
        cur_observation = cur_state
        n_observation = self.normalize_obs(cur_observation)
        
        rollout_length = targets.shape[0]
        
        rwds = []
        errs = []
        
        from tqdm import tqdm
        
        survival_time = 0
        fail = False
        
        # calculate trajectory ##
        
        loop = range(rollout_length) if 'progress' not in model_kwargs else tqdm(range(rollout_length))
        for i in loop:
            target = targets[i]
            if 'noise' in model_kwargs and model_kwargs['noise']:
                target_noisy = target + np.random.random_integers(1, 10000, target.shape) / 10000 * model_kwargs['noise']
                action, info = self.act_tracking(n_observation = n_observation, target = target_noisy)
            else:
                action, info = self.act_tracking(n_observation = n_observation, target = target)
            
            action = ptu.to_numpy(action).flatten()
            new_observation, rwd, done, info = env.step(action, random=False)
            saver.append_no_root_to_buffer()
            rwd = self.cal_rwd(observation=new_observation['observation'], target=targets[i])
            rwds.append(rwd)
            err = self.cal_err(observation = new_observation['observation'], target = targets[i])
            errs.append(err)
            cur_state = new_observation['state']
            # cur_observation = state2ob(cur_state)
            cur_observation  = cur_state
            n_observation = self.normalize_obs(cur_observation)
            if done:
                fail = True
            if not fail:
                survival_time += 1
          
        return rwds, errs, fail, survival_time
