import logging
from typing import Optional
from random import sample, uniform

import gpytorch
import numpy as np
import torch
from botorch import fit_gpytorch_mll
from botorch.exceptions import ModelFittingError
from botorch.models import SingleTaskGP, FixedNoiseGP
from gpytorch import ExactMarginalLogLikelihood
from gpytorch.kernels import ScaleKernel
from gpytorch.likelihoods import GaussianLikelihood
from torch import Tensor

from bounce import settings
from bounce.kernel.categorical_mixture import MixtureKernel
from bounce.projection import AxUS
from bounce.util.benchmark import ParameterType

from envs.params import GP_PARAM as p

def get_gp(
    axus: AxUS,
    x: Tensor,
    fx: Tensor,
    # fx_var: Tensor,
    lengthscale_prior_shape: float = p["lengthscale_prior_shape"],
    lengthscale_prior_rate: float = p["lengthscale_prior_rate"],
    outputscale_prior_shape: float = p["outputscale_prior_shape"],
    outputscale_prior_rate: float = p["outputscale_prior_rate"],
    noise_prior_shape: float = p["noise_prior_shape"],
    noise_prior_rate: float = p["noise_prior_rate"],
    lamda: Optional[float] = None,
    discrete_ard: bool = False,
    continuous_ard: bool = True,
    neighbor_distance: float = 0.01,
    pseudo_point_mode: float = True,
    pseudo_point_ratio: float = 1.0,
    # gp_mode: str = 'fixednoisegp'
) -> tuple[SingleTaskGP, Tensor, Tensor]:
    """
    Define the GP model.

    Args:
        axus: the AxUS object
        x: the input points
        fx: the function values at the input points
        lengthscale_prior_shape: the shape parameter of the lengthscale prior
        lengthscale_prior_rate: the rate parameter of the lengthscale prior
        outputscale_prior_shape: the shape parameter of the outputscale prior
        outputscale_prior_rate: the rate parameter of the outputscale prior
        noise_prior_shape: the shape parameter of the noise prior
        noise_prior_rate: the rate parameter of the noise prior
        lamda: the parameter for the weighted average in the mixturekernel. trainable if set to None
        discrete_ard: whether to use ARD for discrete parameters
        continuous_ard: whether to use ARD for continuous parameters

    Returns:
        the GP model, the input points, and the function values at the input points

    """

    assert not discrete_ard, "ARD for discrete parameters is not supported yet"
    assert continuous_ard, "ARD for continuous parameters is always used"
    continuous_type = axus.bins_and_indices_of_type(ParameterType.CONTINUOUS) +\
                        axus.bins_and_indices_of_type(ParameterType.NUMERICAL)
    continuous_dims = np.array([ i.item() for ( _, i ) in continuous_type])
    # continuous_dims = np.asarray(
    #     [i.item() for b, i in axus.bins_and_indices_of_type(ParameterType.CONTINUOUS)]
    # )
    discrete_dims = np.setdiff1d(np.arange(axus.target_dim), continuous_dims)

    if len(discrete_dims) == 0:
        kernel = gpytorch.kernels.MaternKernel(
            nu=2.5,
            ard_num_dims=axus.target_dim,
            lengthscale_prior=gpytorch.priors.GammaPrior(
                lengthscale_prior_shape, lengthscale_prior_rate
            ),
            # botorch 3,6
        )
    elif len(continuous_dims) == 0:
        kernel = gpytorch.kernels.MaternKernel(
            nu=2.5,
            ard_num_dims=None,
            lengthscale_prior=gpytorch.priors.GammaPrior(
                lengthscale_prior_shape, lengthscale_prior_rate
            ),
            # botorch 3,6
        )
    else:
        kernel = MixtureKernel(
            discrete_dims=discrete_dims.tolist(),
            continuous_dims=continuous_dims.tolist(),
            discrete_lengthscale_prior=gpytorch.priors.GammaPrior(
                lengthscale_prior_shape, lengthscale_prior_rate
            ),
            continuous_lengthscale_prior=gpytorch.priors.GammaPrior(
                lengthscale_prior_shape, lengthscale_prior_rate
            ),
            lamda=lamda,
        )

    covar_module = ScaleKernel(
        # Use the same lengthscale prior as in the TuRBO paper
        kernel,
        outputscale_prior=gpytorch.priors.GammaPrior(
            outputscale_prior_shape, outputscale_prior_rate
        ),
        # 1.5, 1, botorch: 2, 0.15
    )

    train_x = x.detach().clone()
    train_fx = fx[:, None].detach().clone()
    # train_fx_var = fx_var.detach().clone() if fx_var is not None else None

    if pseudo_point_mode:
        ###############PSEUDO-POINTS###############
        ###############Developed by TuRBO##################################
        # pseudo_point_x = torch.zeros(train_x.shape)
        # pseudo_point_fx = torch.zeros(train_fx.shape)
        
        # choose_number = sample(range(0, len(train_x)), len(train_x) - 1)
        
        # for n in choose_number:
        #     pseudo_point_x[n] = train_x[n]
        #     pseudo_point_fx[n] = train_fx[n]
        ##################################################################
        
        ################Developed by me###################################
        _, indices = torch.sort(train_fx, dim=0)
        n_pp = int(len(indices) * pseudo_point_ratio)
        best_indices = indices[:n_pp]
        
        pseudo_point_x = train_x[best_indices].squeeze().clone()
        pseudo_point_fx = train_fx[best_indices].squeeze(1).clone()
        ##################################################################
        
        for x, fx in zip(pseudo_point_x, pseudo_point_fx):
            for _ in range(len(x)):
                x[_] = uniform(x[_] - neighbor_distance, x[_] + neighbor_distance)
                
        train_x_pp = torch.cat((train_x, pseudo_point_x))
        train_fx_pp = torch.cat((train_fx, pseudo_point_fx))
        
        # Define the model
        likelihood = GaussianLikelihood(
            noise_prior=gpytorch.priors.GammaPrior(noise_prior_shape, noise_prior_rate)
        )
        
        model = SingleTaskGP(
                train_X=train_x_pp,
                train_Y=train_fx_pp,
                covar_module=covar_module,
                likelihood=likelihood,
            )
        \
        # if gp_mode == 'singletaskgp':
        #     model = SingleTaskGP(
        #         train_X=train_x_pp,
        #         train_Y=train_fx_pp,
        #         covar_module=covar_module,
        #         likelihood=likelihood,
        #     )
        # elif gp_mode == 'fixednoisegp':
        #     logging.info("🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳🍳")
        #     pseudo_point_fx_var = torch.zeros(train_fx_var.shape)
        #     for n in choose_number:
        #         pseudo_point_fx_var[n] = train_fx_var[n]
        #     train_fx_var_pp = torch.cat((train_fx_var, pseudo_point_fx_var))    
                
        #     model = FixedNoiseGP(
        #         train_X=train_x_pp,
        #         train_Y=train_fx_pp,
        #         train_Yvar=train_fx_var_pp,
        #         covar_module=covar_module,
        #     )
        # else:
        #     assert False, "Failed to define gp_mode : singletaskgp or fixednoisegp"
    ###########################################
    else:
        # Define the model
        likelihood = GaussianLikelihood(
            noise_prior=gpytorch.priors.GammaPrior(noise_prior_shape, noise_prior_rate)
        )
        model = SingleTaskGP(
                train_X=train_x,
                train_Y=train_fx,
                covar_module=covar_module,
                likelihood=likelihood,
            )
        # if gp_mode == 'singletaskgp':
        #     model = SingleTaskGP(
        #         train_X=train_x,
        #         train_Y=train_fx,
        #         covar_module=covar_module, 
        #         likelihood=likelihood,
        #     )
        # elif gp_mode == 'fixednoisegp':
        #     model = FixedNoiseGP(
        #         train_X=train_x,
        #         train_Y=train_fx,
        #         train_Yvar=train_fx_var,
        #         covar_module=covar_module,
        #     )
        # else:
        #     assert False, "Failed to define gp_mode : singletaskgp or fixednoisegp"
        
    return model, train_x, train_fx


def fit_mll(
    model: SingleTaskGP,
    train_x: Tensor,
    train_fx: Tensor,
    max_cholesky_size: int = 1000,
    use_scipy_lbfgs: bool = True,
) -> None:
    """
    Fit the GP model. If the LBFGS optimizer fails, use the Adam optimizer.

    Args:
        model: the GP model
        train_x: the input points
        train_fx: the function values at the input points
        max_cholesky_size: the maximum size of the Cholesky decomposition
         use_scipy_lbfgs: whether to use the scipy LBFGS optimizer, otherwise use the Adam optimizer

    Returns:
        None

    """
    # Set model to training mode
    model.train()
    model.likelihood.train()
    mll = ExactMarginalLogLikelihood(model.likelihood, model)
    with gpytorch.settings.max_cholesky_size(max_cholesky_size):
        lbgs_failed = False
        if use_scipy_lbfgs:
            try:
                fit_gpytorch_mll(
                    mll=mll,
                    model=model,
                    train_x=train_x,
                    train_fx=train_fx,
                )
                model.eval()
            except ModelFittingError:
                lbgs_failed = True

        if not use_scipy_lbfgs or lbgs_failed:
            if lbgs_failed:
                logging.warning(
                    "⚠ Failed to fit GP using LBFGS, using backup Adam optimizer"
                )
            optimizer = torch.optim.Adam([{"params": model.parameters()}], lr=0.1)

            for _ in range(settings.MLL_FITTING_ITERATIONS):
                optimizer.zero_grad()
                output = model(train_x)
                loss = -mll(output, train_fx.flatten())
                loss.backward()
                optimizer.step()

    model.eval()
    model.likelihood.eval()
