import gc
import os

import numpy as np
import psutil
import torch
from torchvision.utils import save_image

from attacks.attack import Attack
import time
from tqdm import tqdm
import cv2

# from ray import tune
# from ray.tune import CLIReporter
# from ray.tune.schedulers import PopulationBasedTraining

from loss import VOCriterion

def get_W(n):
    W = [0, 0.22]

    while W[-1] < 1:
        W.append(W[-1] + max(0.06, W[-2] - W[-1] - 0.03))

    W = set(map(lambda x: np.ceil(x * n).item() if x < 1 else 0, W))
    return W

class PGD(Attack):
    def __init__(
            self,
            model,
            criterion,
            test_criterion,
            data_shape,
            norm='Linf',
            n_iter=20,
            n_restarts=1,
            alpha=None,
            rand_init=False,
            sample_window_size=None,
            sample_window_stride=None,
            pert_padding=(0, 0),
            init_pert_path=None,
            init_pert_transform=None):
        super(PGD, self).__init__(model, criterion, test_criterion, norm, data_shape,
                                  sample_window_size, sample_window_stride,
                                  pert_padding)

        self.alpha = alpha

        self.n_restarts = n_restarts
        self.n_iter = n_iter

        self.rand_init = rand_init

        self.init_pert = None
        if init_pert_path is not None:
            self.init_pert = cv2.cvtColor(cv2.imread(init_pert_path), cv2.COLOR_BGR2RGB)
            if init_pert_transform is None:
                self.init_pert = torch.tensor(self.init_pert).unsqueeze(0)
            else:
                self.init_pert = init_pert_transform({'img': self.init_pert})['img'].unsqueeze(0)


    def calc_sample_grad_single(self, pert, img1_I0, img2_I0, intrinsic_I0, img1_delta, img2_delta,
                                scale, y, clean_flow, target_pose, perspective1, perspective2, mask1, mask2,
                                device=None, req_F=False):
        pert = pert.detach()
        pert.requires_grad_()
        img1_adv, img2_adv, output_adv = self.perturb_model_single(pert, img1_I0, img2_I0,
                                                                   intrinsic_I0,
                                                                   img1_delta, img2_delta,
                                                                   scale,
                                                                   mask1, mask2,
                                                                   perspective1,
                                                                   perspective2,
                                                                   device)
        loss = self.criterion(output_adv, scale.to(device), y.to(device), target_pose.to(device), clean_flow.to(device))
        loss_sum = loss.sum(dim=0)
        grad = torch.autograd.grad(loss_sum, [pert])[0].detach()

        del img1_adv
        del img2_adv
        del output_adv
        del loss
        if not req_F:
            del loss_sum
        torch.cuda.empty_cache()

        if not req_F:
            return grad
        else:
            return grad, loss_sum

    
    def calc_sample_grad_split(self, pert, img1_I0, img2_I0, intrinsic_I0, img1_delta, img2_delta,
                               scale, y, clean_flow, target_pose, perspective1, perspective2, mask1, mask2, device=None,
                               req_F=False):
        sample_data_ind = list(range(img1_I0.shape[0] + 1))
        window_start_list = sample_data_ind[0::self.sample_window_stride]
        window_end_list = sample_data_ind[self.sample_window_size::self.sample_window_stride]

        if window_end_list[-1] != sample_data_ind[-1]:
            window_end_list.append(sample_data_ind[-1])
        grad = torch.zeros_like(pert, requires_grad=False)
        grad_multiplicity = torch.zeros(grad.shape[0], device=grad.device, dtype=grad.dtype)

        if req_F:
            loss_tot = 0

        for window_idx, window_end in enumerate(window_end_list):
            window_start = window_start_list[window_idx]
            grad_multiplicity[window_start:window_end] += 1

            pert_window = pert[window_start:window_end].clone().detach()
            img1_I0_window = img1_I0[window_start:window_end].clone().detach()
            img2_I0_window = img2_I0[window_start:window_end].clone().detach()
            intrinsic_I0_window = intrinsic_I0[window_start:window_end].clone().detach()
            img1_delta_window = img1_delta[window_start:window_end].clone().detach()
            img2_delta_window = img2_delta[window_start:window_end].clone().detach()
            scale_window = scale[window_start:window_end].clone().detach()
            y_window = y[window_start:window_end].clone().detach()
            clean_flow_window = clean_flow[window_start:window_end].clone().detach()
            target_pose_window = target_pose.clone().detach()
            perspective1_window = perspective1[window_start:window_end].clone().detach()
            perspective2_window = perspective2[window_start:window_end].clone().detach()
            mask1_window = mask1[window_start:window_end].clone().detach()
            mask2_window = mask2[window_start:window_end].clone().detach()

            if not req_F:
                grad_window = self.calc_sample_grad_single(pert_window,
                                                           img1_I0_window,
                                                           img2_I0_window,
                                                           intrinsic_I0_window,
                                                           img1_delta_window,
                                                           img2_delta_window,
                                                           scale_window,
                                                           y_window,
                                                           clean_flow_window,
                                                           target_pose_window,
                                                           perspective1_window,
                                                           perspective2_window,
                                                           mask1_window,
                                                           mask2_window,
                                                           device=device)
            else:
                grad_window, loss_sum = self.calc_sample_grad_single(pert_window,
                                                                     img1_I0_window,
                                                                     img2_I0_window,
                                                                     intrinsic_I0_window,
                                                                     img1_delta_window,
                                                                     img2_delta_window,
                                                                     scale_window,
                                                                     y_window,
                                                                     clean_flow_window,
                                                                     target_pose_window,
                                                                     perspective1_window,
                                                                     perspective2_window,
                                                                     mask1_window,
                                                                     mask2_window,
                                                                     device=device, req_F=True)
            with torch.no_grad():
                grad[window_start:window_end] += grad_window
                if req_F:
                    loss_tot += loss_sum

            del grad_window
            del pert_window
            del img1_I0_window
            del img2_I0_window
            del intrinsic_I0_window
            del scale_window
            del y_window
            del clean_flow_window
            del target_pose_window
            del perspective1_window
            del perspective2_window
            del mask1_window
            del mask2_window
            torch.cuda.empty_cache()
        grad_multiplicity_expand = grad_multiplicity.view(-1, 1, 1, 1).expand(grad.shape)
        grad = grad / grad_multiplicity_expand
        del grad_multiplicity
        del grad_multiplicity_expand
        torch.cuda.empty_cache()

        if not req_F:
            return grad.to(device)
        else:
            return grad.to(device), loss_tot


    def perturb(self, data_loader, y_list, eps,
                                   targeted=False, device=None, eval_data_loader=None, eval_y_list=None):

        self.criterion = VOCriterion(t_crit='mean_partial_rms',
                                    rot_crit='mean_partial_rms',
                                    flow_crit='cosine_similarity',
                                    target_t_crit='none',
                                    t_factor=0.6,
                                    rot_factor=0.3,
                                    flow_factor=0.1,
                                    target_t_factor=0)
        self.test_criterion = VOCriterion(
            t_crit='mean_partial_rms',
            rot_crit='none',
            flow_crit='none',
            target_t_crit='none',
            t_factor=1.0,
            rot_factor=0,
            flow_factor=0,
            target_t_factor=0
        )

        a_abs = np.abs(eps / self.n_iter) if self.alpha is None else np.abs(self.alpha)
        multiplier = -1 if targeted else 1
        print("computing PGD attack with parameters:")
        print("attack random restarts: " + str(self.n_restarts))
        print("attack epochs: " + str(self.n_iter))
        print("attack norm: " + str(self.norm))
        print("attack epsilon norm limitation: " + str(eps))
        print("attack step size: " + str(a_abs))

        data_shape, dtype, eval_data_loader, eval_y_list, clean_flow_list, \
        eval_clean_loss_list, traj_clean_loss_mean_list, clean_loss_avg, \
        best_pert, best_loss_list, best_loss_avg, all_loss, all_best_loss = \
            self.compute_clean_baseline(data_loader, y_list, eval_data_loader, eval_y_list, device=device)
        W = get_W(self.n_iter)
        for rest in tqdm(range(self.n_restarts)):
            print("restarting attack optimization, restart number: " + str(rest))
            opt_start_time = time.time()
            pert = torch.zeros_like(best_pert)

            if self.init_pert is not None:
                print(" perturbation initialized from provided image")
                pert = self.init_pert.to(best_pert)
            elif self.rand_init:
                print(" perturbation initialized randomly")
                pert = self.random_initialization(pert, eps)
            else:
                print(" perturbation initialized to zero")

            pert = self.project(pert, eps)

            # momentum
            g_prev = self.gradient_ascent_step(pert, data_shape, data_loader, y_list, clean_flow_list,
                                              multiplier, a_abs, eps, requires_grad=True, d=None, device=device)
            x = self.project(pert + a_abs * g_prev, eps)
            del g_prev
            x_max = x
            f_max = self.attack_eval(x_max, data_shape, data_loader, y_list,
                                    device, isTrain=True)
            lr_changed = False
            improved = 0
            since_last_check = 0
            d = {
                "iter": -1,
                "since_last_check": since_last_check,
                "x_prev": pert,
                "x": x,
                "x_max": x_max,
                "f_max": f_max,
                "improved": improved,
                "lr": a_abs,
                "lr_changed": lr_changed,
                "W": W,
                "rest": 0
            }

            for k in tqdm(range(self.n_iter)):
                if psutil.virtual_memory().percent >= 80.0:
                    gc.collect()
                torch.cuda.empty_cache()
                print(" attack optimization epoch: " + str(k))
                iter_start_time = time.time()

                d = self.gradient_ascent_step(d["x"], data_shape, data_loader, y_list, clean_flow_list,
                                             multiplier, a_abs, eps, d=d, device=device)
                pert = d["x"]

                step_runtime = time.time() - iter_start_time
                print(" optimization epoch finished, epoch runtime: " + str(step_runtime))

                print(" evaluating perturbation")
                eval_start_time = time.time()

                with torch.no_grad():
                    avg_last_loss, eval_loss_list = self.attack_eval(pert, data_shape, eval_data_loader, eval_y_list,
                                                                     device, avg=True)

                    if avg_last_loss > best_loss_avg:
                        best_pert = pert.clone().detach()
                        best_loss_list = eval_loss_list
                        best_loss_avg = avg_last_loss
                        os.makedirs("temp_best", exist_ok=True)
                        save_image(best_pert[0], f"temp_best/{k}_adv_best_pert.png")

                    all_loss.append(eval_loss_list)
                    all_best_loss.append(best_loss_list)
                    traj_loss_mean_list = np.mean(eval_loss_list, axis=0)
                    traj_best_loss_mean_list = np.mean(best_loss_list, axis=0)

                    eval_runtime = time.time() - eval_start_time
                    print(" evaluation finished, evaluation runtime: " + str(eval_runtime))
                    print(" current trajectories loss mean list:")
                    print(" " + str(traj_loss_mean_list))
                    print(" current trajectories best loss mean list:")
                    print(" " + str(traj_best_loss_mean_list))
                    print(" trajectories clean loss mean list:")
                    print(" " + str(traj_clean_loss_mean_list))
                    print("current average last frame loss:")
                    print(avg_last_loss)
                    print(" current trajectories best loss avg:")
                    print(" " + str(best_loss_avg))
                    print(" trajectories clean loss avg:")
                    print(" " + str(clean_loss_avg))
                    #del eval_loss_tot
                    del eval_loss_list
                    torch.cuda.empty_cache()

            opt_runtime = time.time() - opt_start_time
            print("optimization restart finished, optimization runtime: " + str(opt_runtime))
        return best_pert.detach(), eval_clean_loss_list, all_loss, all_best_loss

