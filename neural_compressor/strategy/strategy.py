#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2021 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The base class for tuning strategy."""

import os
import math
import copy
import pickle
import yaml
import sys

from abc import abstractmethod
from copy import deepcopy
from collections import OrderedDict, defaultdict
from pathlib import Path
from time import time
import numpy as np

from neural_compressor.adaptor.tensorflow import TensorFlowAdaptor
from ..config import MixedPrecisionConfig, options
from ..objective import MultiObjective
from ..adaptor import FRAMEWORKS
from ..utils.utility import Statistics, fault_tolerant_file, GLOBAL_STATE, MODE, LazyImport, DotDict
from ..utils.create_obj_from_config import create_eval_func
from ..utils import logger
from ..version import __version__
from ..algorithm import AlgorithmScheduler, ALGORITHMS

from .utils.tuning_space import  TuningSpace
from .utils.tuning_structs import OpTuningConfig
from .utils.constant import FALLBACK_RECIPES_SET


STRATEGIES = {}


def strategy_registry(cls):
    """Class decorator used to register all TuneStrategy subclasses.

    Args:
        cls (class): The class of register.

    Returns:
        cls: The class of register.
    """
    assert cls.__name__.endswith(
        'TuneStrategy'
    ), "The name of subclass of TuneStrategy should end with \'TuneStrategy\' substring."
    if cls.__name__[:-len('TuneStrategy')].lower() in STRATEGIES:
        raise ValueError('Cannot have two strategies with the same name')
    STRATEGIES[cls.__name__[:-len('TuneStrategy')].lower()] = cls
    return cls

@strategy_registry
class TuneStrategy(object):
    """Basic class for tuning strategy."""

    def __init__(self,
                 model,
                 conf,
                 q_dataloader=None,
                 q_func=None,
                 eval_func=None,
                 eval_dataloader=None,
                 eval_metric=None,
                 resume=None,
                 q_hooks=None):
        """Init the TuneStrategy.

        Args:
            model: The FP32 model specified for low precision tuning.
            conf: The Conf class instance includes all user configurations.
            q_dataloader: Data loader for calibration, mandatory for post-training quantization.  Defaults to None.
            q_func: Training function for quantization aware training. Defaults to None. Defaults to None.
            eval_func: The evaluation function provided by user. This function takes model as parameter, and
                evaluation dataset and metrics should be encapsulated in this function implementation and
                outputs a higher-is-better accuracy scalar value.
            eval_dataloader: Data loader for evaluation. Defaults to None.
            eval_metric: Metric for evaluation. Defaults to None.
            resume: The dict containing resume information. Defaults to None.
            q_hooks: The dict of training hooks, supported keys are: on_epoch_begin, on_epoch_end, on_step_begin,
                on_step_end. Their values are functions to be executed in adaptor layer.. Defaults to None.
        """
        self.model = model
        self.conf = conf
        self.config = self._initialize_config(conf)
        self.history_path = self._create_path(options.workspace, './history.snapshot')
        self.deploy_path = self._create_path(options.workspace, 'deploy.yaml')
        self.calib_dataloader = q_dataloader
        self.eval_func = eval_func
        self.eval_dataloader = eval_dataloader
        self.eval_metric = eval_metric
        # not tuning equals to performance only in 1.x
        self._not_tuning = True
        self._check_tuning_status()
        self.q_func = q_func
        self.q_hooks = q_hooks
        GLOBAL_STATE.STATE = MODE.QUANTIZATION
        framework, framework_specific_info = self._set_framework_info(q_dataloader, q_func)
        self.adaptor = FRAMEWORKS[framework](framework_specific_info)
        self.framework = framework

        self.set_q_func()
        self._set_objectives()
        self.tune_data = {}
        self.tune_result_record = []
        self.tuning_history = []
        self.tuning_result_data = []

        self.baseline = None
        self.last_tune_result = None
        self.last_qmodel = None
        self.last_tune_cfg = None
        self.best_qmodel = None
        self.best_tune_result = None
        # track the best tuning config correspondence to the best quantized model
        self.best_tuning_cfg = None
        # track the current best accuracy
        self.cur_best_acc = self.initial_best_acc()
        # track tuning cfg with the current best accuracy
        self.cur_best_tuning_cfg = {}
        self.re_quant = False
        self.trials_count = 0

        # query capability and build tuning space
        self.capability = self.adaptor.query_fw_capability(model)
        logger.debug(self.capability)
        self.set_tuning_space(self.config)

        # set algo scheduler
        self.algo_scheduler = AlgorithmScheduler(self.config.recipes)
        # reuse the calibration iteration
        self.algo_scheduler.dataloader = self.calib_dataloader
        self.algo_scheduler.origin_model = self.model
        self.algo_scheduler.adaptor = self.adaptor

        self._optype_statistics = None
        self.fallback_stats_baseline = None
        self.fallback_stats = None
        self.tuning_times = 0
        self.fallback_start_point = 0
        self.metric_met_point = 0

        # set recipes
        # recipe name -> the list of supported value
        self._tuning_recipes = OrderedDict()
        # recipe name-> the default value when not tuning
        self._tuning_recipes_default_values = {}
        # recipe name -> the value specified by user
        self._not_tuning_recipes_values = {}
        self._initialize_recipe()
        self.applied_all_recipes_flag = False

        self._resume = resume
        if self._resume is not None: self.setup_resume(resume)

    def _check_tuning_status(self):
        # got eval func
        if self.eval_func:
            self._not_tuning = False
            logger.info("Execute the tuning process due to detect the evaluation function.")
            if self.eval_dataloader:
                logger.warning("Ignore the evaluation dataloader due to evaluation function exist.")
            if self.eval_metric:
                logger.warning("Ignore the evaluation metric due to evaluation function exist.")
            return
        # got eval dataloader + eval metric => eval func
        if self.eval_dataloader and self.eval_metric:
            self._not_tuning = False
            logger.info("Create evaluation function according to evaluation dataloader and metric\
                and Execute the tuning process.")
            return
        else:
            # got eval dataloader but not eval metric
            if self.eval_dataloader:
                assert self.eval_metric, "Detected evaluation dataloader but no evaluation metric, " \
                 "Please provide both to perform tuning process or neither for the default quantization."
            # got eval metric but not eval dataloader
            if self.eval_metric:
                assert self.eval_dataloader, "Detected evaluation metric but no evaluation dataloader, "\
                    "Please provide both to perform tuning process or neither for the default quantization."
        # not tuning
        if self._not_tuning:
            logger.info("Quantize the model with default configuration without evaluating the model.\
                To perform the tuning process, please either provide an eval_func or provide an\
                    eval_dataloader an eval_metric.")

    def _initialize_config(self, conf):
        """Init the tuning config based on user conf.

        Args:
            conf: User config

        Returns:
            Tuning config
        """
        config = conf.quantization
        return config

    @abstractmethod
    def next_tune_cfg(self):
        """Interface for generate the next tuning config.

        The generator of yielding next tuning config to traverse by concrete strategies or quantization level
        according to last tuning result and traverse logic.

        It should be implemented by the sub-class.

        Yields:
            tune_config (dict): It's a dict containing the tuning configuration to traverse.
        """
        raise NotImplementedError

    def traverse(self):
        """Traverse the tuning space.

        The main traverse logic which could be override by some concrete strategy which needs more hooks.
        """
        self._eval_baseline()
        if self.config.use_distributed_tuning:
            logger.info("use distributed traverse: {}".format(self.config.use_distributed_tuning))
            return self.distributed_traverse()
        traverse_start_time = time()
        for op_tuning_cfg in self.next_tune_cfg():
            tuning_start_time = time()
            tune_cfg = self._tune_cfg_converter(op_tuning_cfg)
            self.trials_count += 1
            tuning_history = self._find_tuning_history(tune_cfg)
            if tuning_history and self.trials_count < self.config.tuning_criterion.max_trials:
                self.last_tune_result = tuning_history['last_tune_result']
                self.best_tune_result = tuning_history['best_tune_result']
                logger.warn("Find evaluated tuning config, skip.")
                continue
            self._remove_redundant_qmodel()
            logger.debug("Dump current tuning configuration:")
            logger.debug(tune_cfg)
            self.tuning_times += 1
            # set the parameter for pre quantization algos and run
            self.set_param_for_pre_quantization_algos(self.algo_scheduler, tune_cfg, self.model)
            self.model = self.algo_scheduler('pre_quantization')
            # quantize
            q_model = self.adaptor.quantize(copy.deepcopy(tune_cfg), self.model, self.calib_dataloader, self.q_func)
            assert self.adaptor.pre_optimized_model
            # set the parameter for post quantization algos and run
            self.set_param_for_post_quantization_algos(self.algo_scheduler, tune_cfg,\
                self.adaptor.pre_optimized_model, q_model)
            self.last_qmodel = self.algo_scheduler('post_quantization')
            self.last_tune_cfg = copy.deepcopy(tune_cfg)
            # remove the reference to model
            self.algo_scheduler.reset_exec_algorithms()
            assert self.last_qmodel
            # return the last quantized model as a result. if not tune.
            if self._not_tuning:
                self.best_qmodel = self.last_qmodel
                self._add_tuning_history(copy.deepcopy(tune_cfg), (-1, [0]), q_config=self.last_qmodel.q_config)
                return
            self.last_tune_result = self._evaluate(self.last_qmodel)
            self.cur_best_acc, self.cur_best_tuning_cfg = self.update_best_op_tuning_cfg(op_tuning_cfg)
            need_stop = self.stop(self.config.tuning_criterion.timeout, self.trials_count)

            # record the tuning history
            saved_tune_cfg = copy.deepcopy(tune_cfg)
            saved_last_tune_result = copy.deepcopy(self.last_tune_result)
            self._add_tuning_history(saved_tune_cfg,
                                    saved_last_tune_result,
                                    q_config=q_model.q_config)
            self.tune_result_record.append(copy.deepcopy(self.last_tune_result))
            self.tune_cfg = tune_cfg
            now_time = time()
            acc_res_msg = ""
            performance_res_msg = ""
            if self.tuning_result_data:
                acc_res_msg = "[ " + "| ".join(self.tuning_result_data[0]) + " ]"
                performance_res_msg = "[ " + "| ".join(self.tuning_result_data[1]) + " ]"
            logger.debug(f"*** The accuracy of last tuning is: {acc_res_msg}")
            logger.debug(f"*** The performance of last tuning is: {performance_res_msg}")
            logger.debug(f"*** The last tuning time: {(now_time - tuning_start_time):.2f} s")
            logger.debug(f"*** The tuning process lasted time: {(now_time - traverse_start_time):.2f} s")

            self._dump_tuning_process_statistics()
            if need_stop:
                if self.re_quant:
                    logger.info("*** Do not stop the tuning process, re-quantize the ops.")
                    continue
                # recover the best quantized model from tuning config
                self._recover_best_qmodel_from_tuning_cfg()
                if self.config.diagnosis:
                    logger.debug(f'*** Start to do diagnosis (inspect tensor).')
                    self._diagnosis()
                if self.use_multi_objective and len(self.tune_result_record) > 1 and \
                    self.best_tune_result is not None:
                    best_trail, best_result = self.objectives.best_result(self.tune_result_record,
                                                                          copy.deepcopy(self.baseline))
                    if best_result != self.best_tune_result:
                        from neural_compressor.utils.utility import recover
                        self.best_qmodel = recover(self.model.model,
                            os.path.join(options.workspace, 'history.snapshot'),
                            best_trail)
                        logger.debug(f"*** Update the best qmodel by recovering from history.")
                        self.best_tune_result = best_result
                    self._dump_tuning_process_statistics()
                break
        self._recover_best_qmodel_from_tuning_cfg()

    def _initialize_recipe(self):
        """Divide the recipe into two categories tuning/not tuning."""
        from .utils.utility import get_adaptor_name
        from ..utils.constant import RECIPES as fwk_recipes
        from ..utils.constant import RECIPES_PRIORITY as fwk_recipes_priority
        # get all recipes supported by adaptor.
        adaptor_name = get_adaptor_name(self.adaptor)
        adaptor_recipes = fwk_recipes['common']
        # TODO WA due to smooth quant only supported by ort/pt currently.
        if not adaptor_name not in ['onnx', 'pytorch']:
            adaptor_recipes.pop('smooth_quant', None)
        for adaptor_name_key, adaptor_recipes_val in fwk_recipes.items():
            if adaptor_name_key.startswith(adaptor_name):
                adaptor_recipes.update(adaptor_recipes_val)
        # divide it into two categories:
        # tuning lst: the value is equal to the default value
        # not tuning list: the value is not equal to the default value
        logger.info(f"Adaptor has {len(adaptor_recipes)} recipes.")
        logger.debug(adaptor_recipes)
        usr_recipes_cfg = self.config.recipes if self.config.recipes else {}
        for recipe_name, recipe_val in usr_recipes_cfg.items():
            # for not tuning recipes, use the value specified by user.
            if recipe_name in adaptor_recipes and recipe_val != adaptor_recipes[recipe_name][0]:
                self._not_tuning_recipes_values[recipe_name] = recipe_val
        # sorted the recipes and set the default value to be used before recipe tuning
        for recipe_name in fwk_recipes_priority:
            if recipe_name in adaptor_recipes and recipe_name not in self._not_tuning_recipes_values:
                # TODO skip tuning smooth_quant first
                if recipe_name == 'smooth_quant': continue
                self._tuning_recipes[recipe_name] = adaptor_recipes[recipe_name]
                self._tuning_recipes_default_values[recipe_name] = adaptor_recipes[recipe_name][0]
        logger.info(f"{len(self._not_tuning_recipes_values)} recipes specified by user.")
        logger.debug(self._not_tuning_recipes_values)
        logger.info(f"{len(self._tuning_recipes)} recipes require future tuning.")
        logger.debug(self._tuning_recipes)

    def distributed_next_tune_cfg_lst(self, comm):
        """Interface for generate the distributed next tuning config list.

        The generator of yielding next tuning config list to distributed traverse by concrete strategies or
        quantization level according to tuning result and traverse logic.

        It should be implemented by the sub-class. Currently, it is only implemented in the BasicTuneStrategy.
        """
        pass

    def meet_acc_req(self, eval_res):
        """Compare the result of last tuning with baseline to check whether the result meet requirements.

        Args:
            eval_res: The evaluation result of tuning.

        Returns:
            Return True if the accuracy meets requirements else False.
        """
        self.last_tune_result = eval_res
        return self.objectives.accuracy_meet_req(deepcopy(self.last_tune_result))

    def master_worker_handle(self, comm):
        """Master worker handles the task assignment and result management.

        Master node send all task ids to all free nodes, and wait until any result.
        When receiving any result, directly send a new task id to the sender (it's free).

        Args:
            comm (MPI.COMM): The instance of communication for MPI.
        """
        MPI = LazyImport("mpi4py.MPI")
        size = comm.Get_size()
        for process_id in range(1, min(len(self.tune_cfg_lst) + 1, size)):
            tune_cfg_id = process_id - 1
            logger.info("[Rank {}]master sending tune cfg: {} to rank {}".format(comm.Get_rank(), \
                tune_cfg_id, process_id))
            comm.send(
                obj=tune_cfg_id, # just send the tune cfg id is enough
                dest=process_id, # rank 0 send to rank 1, 2, ...
                tag=tune_cfg_id # tag, the index of tune cfg 0,1,2,3
            )
            import time as ttime
            # WA for UT
            ttime.sleep(0.5)

        # master should be aware of the next config id to send
        cur_cfg_id = min(len(self.tune_cfg_lst), size - 1)
        # WA for UT
        self.eval_results = {}
        # record number of all response acks, break when it equals to len()
        self.num_acks = 0
        # used to obtain the source and the tag for each received message
        status = MPI.Status()

        self.already_ack_id_lst = set()
        self.requirements_met_min_cfg_id = sys.maxsize

        # stuck here to receive any result
        while True:
            eval_res = comm.recv(
                source=MPI.ANY_SOURCE,
                tag=MPI.ANY_TAG,
                status=status   # get MPI status object
            )
            self.num_acks += 1
            # sender rank
            sender_rank = status.Get_source()
            # the task id that is finished
            tag = status.Get_tag()

            logger.info("[Rank {}]master receiving eval result: {} from rank {}".format(comm.Get_rank(), \
                eval_res, sender_rank))

            # record eval_results for context coordination of stage 3
            self.last_tune_result = eval_res
            self.eval_results[tag] = eval_res

            self.overall_trials += 1
            self.best_tune_cfg_id = None
            self.already_ack_id_lst.add(tag)

            # if meet accuracy requirement, then update minimum id that met requirement
            if(self.meet_acc_req(eval_res)):
                logger.info("[Rank {}]master has one tuning cfg meet acc: {}".format(comm.Get_rank(), tag))
                self.met_flag = True
                self.requirements_met_min_cfg_id = min(self.requirements_met_min_cfg_id, tag)

                # must ensure every id lower than current min_id has been acknowledged
                # because a tune cfg (not acked yet) with lower id can have better acc
                for i in range(self.requirements_met_min_cfg_id):
                    if i not in self.already_ack_id_lst:
                        logger.info("[Rank {}]master has one tuning cfg meet acc: {} but not collect all acks before"\
                                    .format(comm.Get_rank(), tag))
                        # not completely collected yet!
                        self.met_flag = False
                        break

                if self.met_flag:
                    # found the best tune cfg!
                    logger.info("[Rank {}]master has one tuning cfg meet acc: {} and also collect all acks before"\
                                .format(comm.Get_rank(), tag))
                    self.best_tune_cfg_id = self.requirements_met_min_cfg_id
            else:
                # get the current best acc but not meet requirements
                logger.info("[Rank {}]master gets the current best acc: {} but not meet requirements"\
                    .format(comm.Get_rank(), tag))
                self.cur_best_acc, self.cur_best_tuning_cfg = self.update_best_op_tuning_cfg(self.tune_cfg_lst[tag])

            if self.best_tune_cfg_id is not None:
                # we find the best tune cfg id that meet requirements!
                logger.info("[Rank {}]master finds best tune cfg id.".format(comm.Get_rank()))
                logger.info(self.best_tune_cfg_id)
                logger.info(self.tune_cfg_lst[self.best_tune_cfg_id])
                break

            # send the next cfg if not exceed max trials
            if self.overall_trials > self.config.tuning_criterion.max_trials:
                self.max_trial_flag = True
            # elif time.time() - self.overall_time_start > self.config.tuning_criterion.timeout:
            #     self.max_time_flag = True
            elif cur_cfg_id < len(self.tune_cfg_lst):
                logger.info("[Rank {}]master sends new tuning cfg {} to rank: {}".format(comm.Get_rank(), \
                    cur_cfg_id, sender_rank))
                comm.send(obj=cur_cfg_id, dest=sender_rank, tag=cur_cfg_id)
                cur_cfg_id += 1
            else:
                logger.info("[Rank {}]All tune configs are sent, no more sending, just collecting..."\
                    .format(comm.Get_rank()))

            # all collected (ack should collected == acks)
            if len(self.tune_cfg_lst) == self.num_acks:
                # all processes ended
                # return self.requirements_met_min_cfg_id  if it has been updated
                if self.requirements_met_min_cfg_id == sys.maxsize:
                    logger.info("[Rank {}]Not found any tune cfg that meet requirements".format(comm.Get_rank()))
                    self.cur_best_tuning_cfg = self.tune_cfg_lst[0] # TODO select cur_best_tuning_cfg
                else:
                    logger.info("[Rank {}]Find best tune cfg id".format(comm.Get_rank()))
                    logger.info(self.requirements_met_min_cfg_id)
                    self.met_flag = True
                    self.best_tune_cfg_id = self.requirements_met_min_cfg_id
                    logger.info(self.tune_cfg_lst[self.best_tune_cfg_id])
                break

        # send END signal to all other slaves
        logger.info("[Rank {}]master sends END signal to all other slaves".format(comm.Get_rank()))
        for process_id in range(1, size):
            logger.info("[Rank {}]master sends END signal to rank: {}".format(comm.Get_rank(), process_id))
            comm.send(
                obj="MET" if self.met_flag else "NOT MET", # send whether met criterion in the current stage
                dest=process_id, # rank 0 send to rank 1, 2, ...
                tag=len(self.tune_cfg_lst)
            )

        if self.best_tune_cfg_id is not None:
            self.best_qmodel = self.adaptor.quantize(
                    copy.deepcopy(self.tune_cfg_lst[self.best_tune_cfg_id]), self.model, self.calib_dataloader, \
                        self.q_func)


    def slave_worker_handle(self, comm):
        """Slave worker handles the task processing.

        When receiving any task id, slave node finds it in self.tune_cfg_lst and run it.
        Then slave node sends back the tune result to master node.

        Args:
            comm (MPI.COMM): The instance of communication for MPI.
        """
        MPI = LazyImport("mpi4py.MPI")
        status = MPI.Status()
        while True:
            task = comm.recv(
                    source=MPI.ANY_SOURCE,
                    tag=MPI.ANY_TAG,
                    status=status   # sender (master)
                )
            cfg_idx = status.Get_tag()
            if status.Get_tag() >= len(self.tune_cfg_lst):
                logger.info("[Rank {}]slave {} receiving END signal in the current stage".format(comm.Get_rank(),\
                    comm.Get_rank()))
                if task == "MET":
                    logger.info("[Rank {}]met criterion in this stage!".format(comm.Get_rank()))
                    self.met_flag = True
                break
            tune_cfg = self.tune_cfg_lst[cfg_idx]

            # set the parameter for pre quantization algos and run
            self.set_param_for_pre_quantization_algos(self.algo_scheduler, tune_cfg, self.model)
            self.model = self.algo_scheduler('pre_quantization')
            # quantize
            q_model = self.adaptor.quantize(copy.deepcopy(tune_cfg), self.model, self.calib_dataloader, self.q_func)
            assert self.adaptor.pre_optimized_model
            # set the parameter for post quantization algos and run
            self.set_param_for_post_quantization_algos(self.algo_scheduler, tune_cfg, self.adaptor.pre_optimized_model,
                                                       q_model)
            self.last_qmodel = self.algo_scheduler('post_quantization')
            self.last_tune_cfg = copy.deepcopy(tune_cfg)
            # Remove the reference to model
            self.algo_scheduler.reset_exec_algorithms()
            assert self.last_qmodel
            self.last_tune_result = self._evaluate(self.last_qmodel)

            # send back the tuning statistics
            logger.debug("[Rank {}]Slave sends back the tuning statistics".format(comm.Get_rank()))
            logger.debug(self.last_tune_result)
            comm.send(
                obj=self.last_tune_result,
                dest=0, # rank 0 send to rank 1, 2, ...
                tag=cfg_idx
            )

    def distributed_traverse(self):
        """Distributed traverse the tuning space.

        The main traverse logic which could be override by some concrete strategy which needs more hooks.
        """
        MPI = LazyImport("mpi4py.MPI")
        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()

        self.met_flag = False
        # whether exceed max trials
        self.max_trial_flag = False
        # whether exceed max time
        self.max_time_flag = False
        self.overall_trials = 0
        self.overall_time_start = time()

        # for all the stages, handle the tune cfg lst
        # the tune cfg lst is generated/yielded each time by distributed_next_self.tune_cfg_lst
        # we must pass the comm to the specific strategy because slaves may not know
        # contexts such as the best_tune_cfg
        # master should make sure slaves have all the contexts needed before going to the next computation stage
        for op_tuning_cfg_lst in self.distributed_next_tune_cfg_lst(comm):
            self.tune_cfg_lst = [self._tune_cfg_converter(op_tuning_cfg) for op_tuning_cfg in op_tuning_cfg_lst]
            if self.tune_cfg_lst == []:
                # skip empty list at some stages
                continue
            if rank == 0:
                self.master_worker_handle(comm)
            else:
                self.slave_worker_handle(comm)
            logger.debug("# if self.met_flag or self.max_trial_flag or self.max_time_flag:" \
                .format(self.met_flag or self.max_trial_flag or self.max_time_flag))
            if self.met_flag or self.max_trial_flag or self.max_time_flag:
                break

    def _fallback_ops(self, tune_cfg, recipe_op_lst, tuning_space):
        """Fallback ops in recipe op list."""
        for op_name_type in recipe_op_lst:
            tune_cfg.update({op_name_type: OpTuningConfig(op_name_type[0], \
                op_name_type[1],'fp32', tuning_space)})
        return tune_cfg

    def apply_all_tuning_recipes(self, tune_cfg):
        """Apply all tunable recipes with their value."""
        tune_cfg['recipe_cfgs'] = tune_cfg.get('recipe_cfgs', {})
        for recipe_name, recipe_val_lst in self._tuning_recipes.items():
            tune_cfg['recipe_cfgs'][recipe_name] = recipe_val_lst[-1]
            if recipe_name in FALLBACK_RECIPES_SET and 'recipes_ops' in self.capability and \
                len(self.capability['recipes_ops'].get(recipe_name, [])) > 0:
                logger.info(f"Applied recipe {recipe_name}.")
                tune_cfg = self._fallback_ops(tune_cfg, self.capability['recipes_ops'][recipe_name],\
                    self.tuning_space)
        return tune_cfg

    def apply_recipe_one_by_one(self, tune_cfg):
        """Apply the tunable recipes one by one.

        For recipes only have two options, apply the last one.
        For recipes with multiple values. such as alpha of smooth quant, apply it one by one.
        """
        for recipe_name, recipe_vals in self._tuning_recipes.items():
            if recipe_name in FALLBACK_RECIPES_SET and 'recipes_ops' in self.capability and \
                len(self.capability['recipes_ops'].get(recipe_name, [])) > 0:
                logger.info(f"Applied recipe {recipe_name} with value {recipe_vals[-1]}")
                new_tune_cfg = self._fallback_ops(copy.deepcopy(tune_cfg), \
                    self.capability['recipes_ops'][recipe_name], self.tuning_space)
                yield new_tune_cfg
            if recipe_name == "smooth_quant":
                sq_args = {'smooth_quant': True}
                if 'recipe_cfgs' not in new_tune_cfg:
                    new_tune_cfg['recipe_cfgs'] = sq_args
                else:
                    new_tune_cfg['recipe_cfgs'].update(sq_args)
                new_tune_cfg['recipe_cfgs'] = sq_args
                yield new_tune_cfg

    def set_param_for_pre_quantization_algos(self, algo_scheduler, tune_cfg, fp32_model) -> None:
        """Set the parameter for pre-quantization algos, such as smooth quantization.

        Args:
            algo_scheduler: algo scheduler
            tune_cfg: the tuning config
            fp32_model: the fp32 model
        """
        algo_scheduler.origin_model = fp32_model
        algo_scheduler.calib_iter = tune_cfg['calib_iteration']
        algo_scheduler.q_model = fp32_model

        recipe_cfgs = tune_cfg.get('recipe_cfgs', None)
        algo_scheduler.reset_exec_algorithms()
        if recipe_cfgs and recipe_cfgs.get('smooth_quant', False):
            # skip assign alpha to sq first.
            # set the alpha to 0.5 by default
            smooth_quant_args = recipe_cfgs.get('smooth_quant_args', {'alpha': 0.5})
            sq_algo = ALGORITHMS()['smooth_quant']
            sq_algo.alpha = smooth_quant_args['alpha']
            if 'folding' not in smooth_quant_args:
                smooth_quant_args['folding'] = True if self.framework in ['pytorch', 'pytorch_fx'] \
                  else False
                logger.info("SmoothQuant args 'folding' is not set, it's {} now.".format(smooth_quant_args['folding']))
                if self.framework == 'pytorch_ipex':
                    smooth_quant_args['folding'] = None # will reset it to True if IPEX version < 2.1.
            sq_algo.folding = smooth_quant_args['folding']
            logger.debug(f"Set smooth quant with alpha {smooth_quant_args['alpha']} as the pre-quantization algo.")
            algo_scheduler.append_algorithm('pre_quantization', sq_algo)


    def set_param_for_post_quantization_algos(self, algo_scheduler, tune_cfg, pre_optimized_model, q_model) -> None:
        """Set the parameter for post-quantization algos, such as bias correction, weight correction.

        Args:
            algo_scheduler:  algo scheduler
            tune_cfg:  the tuning config.
            pre_optimized_model: the pre-optimized model
            q_model: the quantized model
        """
        algo_scheduler.origin_model = pre_optimized_model
        # if no pre-process algos, return the fp32 model directly.
        algo_scheduler.q_model = q_model

        algo_scheduler.reset_exec_algorithms()
        recipe_cfgs = tune_cfg.get('recipe_cfgs', None)
        # for fast_bias_correction
        if recipe_cfgs and recipe_cfgs.get('fast_bias_correction', False):
            fbc_algo = ALGORITHMS()['fast_bias_correction']
            fbc_algo.quantization_cfg = deepcopy(tune_cfg)
            algo_scheduler.append_algorithm('post_quantization', fbc_algo)
            logger.debug(f"Add fast bias correction as the post quantization algo.")
        # for weight correction
        if recipe_cfgs and recipe_cfgs.get('weight_correction', False):
            w_algo = ALGORITHMS()['weight_correction']
            w_algo.quantization_cfg = deepcopy(tune_cfg)
            algo_scheduler.append_algorithm('post_quantization', w_algo)
            logger.debug(f"Add weight correction as the post quantization algo.")

    def _remove_redundant_qmodel(self):
        """Remove the redundant quantized model to reduce memory use.

        During the tuning process, the strategy only keeps the best tuning config
        instead of the best quantized model to reduce memory use.
        """
        self.last_qmodel = None
        self.best_qmodel = None

    def _eval_baseline(self):
        """Evaluate the fp32 model if needed."""
        if self._not_tuning:

            logger.info("Do not evaluate the baseline and quantize the model with default configuration.")
            return
        else:
            # get fp32 model baseline
            if self.baseline is None:
                logger.info("Get FP32 model baseline.")
                self._fp32_model = self.model
                self.baseline = self._evaluate(self.model)
                self.objectives.baseline = self.baseline
                # record the FP32 baseline
                self._add_tuning_history()
            self.show_baseline_info()

    def _recover_best_qmodel_from_tuning_cfg(self):
        """Recover the best quantized model from tuning config."""
        if self.best_tuning_cfg and not self.best_qmodel:
            self.best_qmodel = self.adaptor.quantize(copy.deepcopy(self.best_tuning_cfg), self.model,
                                                     self.calib_dataloader, self.q_func)

    def _fallback_started(self):
        self.fallback_start_point = self.tuning_times

    def _update_optype_statistics(self):
        self._optype_statistics = defaultdict(lambda:defaultdict(int))

        for op_name_type, op_tune_cfg in self.tune_cfg['op'].items():
            optype = op_name_type[1]
            quant_mode = op_tune_cfg['activation']['quant_mode']
            if isinstance(quant_mode, tuple) or isinstance(quant_mode, list):
                quant_mode = quant_mode[0]
            dtype = 'INT8' if quant_mode in ('static', 'dynamic') \
                    else quant_mode.upper()
            self._optype_statistics[optype]['Total'] += 1
            self._optype_statistics[optype][dtype] += 1
        return

    def _dump_tuning_process_statistics(self):
        self._update_optype_statistics()

        logger.debug("Current tuning process statistics:")
        logger.debug(f"Total Tuning Times: {self.tuning_times}")
        logger.debug("Fallback started at Tune {}".format(self.fallback_start_point))
        logger.debug("Objective(s) met at Tune {}".format(self.metric_met_point))

        fallback_stats = self._calculate_fallback_op_count()
        if self.fallback_stats_baseline == None:
            self.fallback_stats_baseline = fallback_stats
        logger.debug(f"Fallbacked ops count: {self.fallback_stats_baseline - fallback_stats}")

        if isinstance(self.adaptor, TensorFlowAdaptor):
            self._compare_optype_statistics()

        return

    def _calculate_fallback_op_count(self, target_dtype='INT8'):
        fallback_stats = defaultdict(int)

        for optype in self._optype_statistics:
            for dtype, count in self._optype_statistics[optype].items():
                fallback_stats[dtype] += count

        return fallback_stats[target_dtype]


    def _compare_optype_statistics(self, fields=None, optypes=None,
                                   skip_fields=None, skip_optypes=None):
        assert(fields == None or skip_fields == None)
        assert(optypes == None or skip_optypes == None)
        if not isinstance(self.adaptor, TensorFlowAdaptor):
            logger.debug("OpType statistics comparation is only available for TensorFlow adaptor.")
            return

        adaptor_statistics = self.adaptor.optype_statistics

        def _field_skipped(field):
            if fields != None:
                return field not in fields
            elif skip_fields != None:
                return field in skip_fields

        def _optype_skipped(optype):
            if optypes != None:
                return optype not in optypes
            elif skip_optypes != None:
                return optype in skip_optypes


        field_names = adaptor_statistics[0][1:]
        adaptor_data = {
            line[0].lower() : {dtype : count for dtype, count in zip(field_names, line[1:])}
        for line in adaptor_statistics[1]}
        strategy_data = self._optype_statistics

        # compare adaptor statistics to strategy statistics
        logger.debug("Statistics difference between adaptor and tuning config:")
        has_difference = False
        difference_count = 0
        for optype in adaptor_data:
            if optype not in strategy_data or _optype_skipped(optype): continue
            for field in field_names:
                if _field_skipped(field): continue
                adaptor_count = adaptor_data[optype][field]
                strategy_count = strategy_data[optype][field]
                if adaptor_count != strategy_count:
                    has_difference = True
                    if field == 'INT8':
                        difference_count += abs(strategy_count - adaptor_count)
                    logger.debug("\t{}: [adaptor: {} | tune_cfg: {}]".format(
                        (optype, field), adaptor_count, strategy_count))
        if not has_difference:
            logger.debug("\tNone")
        logger.debug(f"\tDifference(s) in total: {difference_count}")
        return

    def initial_tuning_cfg(self):
        """Init the tuning config.

        Initialize the tuning config according to the quantization approach.

        Returns:
            op_item_dtype_dict (OrderedDict): key is (op_name, op_type); value is quantization mode.
            quant_mode_wise_items (OrderedDict): key is quant_mode/precision; value is item list.
            initial_op_tuning_cfg (OrderedDict): key is (op_name, op_type); value is the initialized tuning config.
        """
        from .utils.constant import auto_query_order, static_query_order, dynamic_query_order
        from .utils.tuning_space import initial_tuning_cfg_with_quant_mode
        if self.config.approach == 'post_training_auto_quant':
            query_order = auto_query_order
        elif self.config.approach == 'post_training_dynamic_quant':
            query_order = dynamic_query_order
        elif self.config.approach == 'post_training_static_quant':
            query_order = static_query_order
        elif self.config.approach == 'quant_aware_training':
            query_order = auto_query_order

        quant_mode_wise_items = OrderedDict() # mode, op_item_lst
        pre_items = set()
        # Collect op items supported the specified mode.
        for quant_mode in query_order:
            items = self.tuning_space.query_items_by_quant_mode(quant_mode)
            filtered_items = list(filter(lambda item: item not in pre_items, items))
            pre_items = pre_items.union(set(items))
            quant_mode_wise_items[quant_mode] = filtered_items

        def initial_op_quant_mode(items_lst, target_quant_mode, op_item_dtype_dict):
            for item in items_lst:
                op_item_dtype_dict[item.name] = target_quant_mode

        op_item_dtype_dict = OrderedDict()
        for quant_mode, quant_mode_items in quant_mode_wise_items.items():
            initial_op_quant_mode(quant_mode_items, quant_mode, op_item_dtype_dict)

        initial_op_tuning_cfg = {}
        for op_name_type, quant_mode in op_item_dtype_dict.items():
            initial_op_tuning_cfg[op_name_type] = initial_tuning_cfg_with_quant_mode(op_name_type,
                                                                                     quant_mode,
                                                                                     self.tuning_space)
        return op_item_dtype_dict, quant_mode_wise_items, initial_op_tuning_cfg

    def show_baseline_info(self):
        """Display the accuracy and duration of the the baseline model."""
        if self.baseline:
            self.tune_data['baseline'] = self.baseline[0] if \
                isinstance(self.baseline[0], list) else [self.baseline[0]]
            for name, data in zip(self.metric_name, self.tune_data['baseline']):
                self.tune_data[name] = [data]
            if self.metric_weight:
                # baseline is weighted accuracy
                self.tune_data['Weighted accuracy'] = \
                    [np.mean(np.array(self.tune_data['baseline']) * self.metric_weight)]
                self.tune_data['baseline'] = self.tune_data['Weighted accuracy']
            baseline_msg = '[Accuracy:' + \
                ''.join([' {:.4f}'.format(i) for i in self.tune_data['baseline']]) + \
                ''.join([', {}: {:.4f}'.format(x,y) for x,y in zip( \
                self.objectives.representation, self.baseline[1]) if x != 'Accuracy']) + ']'
        else: # pragma: no cover
            if self.metric_weight:
                self.tune_data['Weighted accuracy'] = ['n/a']
            self.tune_data['baseline'] = ['n/a']

            for name, data in zip(self.metric_name, self.tune_data['baseline']):
                self.tune_data[name] = ['n/a']
            baseline_msg = 'n/a'
        logger.info("FP32 baseline is: {}".format(baseline_msg))

    def initial_best_acc(self):
        """Init the best accuracy.

        Returns:
            The initial value of best accuracy.
        """
        if len(self.metric_name) == 1 or self.metric_weight is not None:
            best_acc = float('-inf') if self.higher_is_better else float('inf')
        else:
            best_acc = [float('-inf') if higher_is_better else float('inf') for \
                        higher_is_better in self.metric_criterion]
        return best_acc

    def _tune_cfg_converter(self, op_tuning_cfg):
        """Convert op_tuning_cfg for adaptor.

        Args:
            op_tuning_cfg (Dict): the op tuning config.
        """
        tune_cfg = {'op': OrderedDict()}
        for op_name_type, op_config in op_tuning_cfg.items():
            if isinstance(op_config, OpTuningConfig):
                tune_cfg['op'][op_name_type] = op_config.get_state()
                op_cap_lst = self.capability['opwise'][op_name_type]
                # Add pattern for diagnosis
                for op_cap in op_cap_lst:
                    if 'pattern' in op_cap:
                        op_pattern = {}
                        op_pattern['sequence'] = op_cap['pattern']['sequence'][0] if\
                            'sequence' in op_cap['pattern'] else None
                        op_pattern['precision'] = op_cap['pattern']['precision'][0] if\
                            'precision' in op_cap['pattern'] else None
                        tune_cfg['op'][op_name_type]['pattern'] = op_pattern
            else:
                tune_cfg[op_name_type] = op_config
        tune_cfg['calib_sampling_size'] = op_tuning_cfg['calib_sampling_size']
        if self.calib_dataloader is not None:
            tune_cfg['calib_iteration'] =  math.ceil(int(tune_cfg['calib_sampling_size']) / \
                                                    self.calib_dataloader.batch_size)
        else:
            tune_cfg['calib_iteration'] = 1
        tune_cfg['approach'] = self.config.approach
        # Add the recipe config
        tune_cfg['recipe_cfgs'] = tune_cfg.get('recipe_cfgs', {})
        # For not tuning recipe, tune cfg use it directly
        tune_cfg['recipe_cfgs'].update(self._not_tuning_recipes_values)
        # WA for get the smooth quant args
        if 'smooth_quant_args' in self.config.recipes:
            tune_cfg['recipe_cfgs']['smooth_quant_args'] = self.config.recipes['smooth_quant_args']
        # For tuning recipe, use the default value if it not specified by recipe tuning sampler.
        for recipe_name, recipe_val in self._tuning_recipes_default_values.items():
            if recipe_name not in tune_cfg['recipe_cfgs']:
                tune_cfg['recipe_cfgs'][recipe_name] = recipe_val
        return tune_cfg

    def set_tuning_space(self, config):
        """Create the tuning space.

        Create the tuning space based on the framework capability and user configuration.

        Args:
            config: The Conf class instance includes all user configurations.
        """
        calib_sampling_size_lst = self.config.calibration_sampling_size
        calib_sampling_size_lst = [int(calib_sampling_size) for calib_sampling_size in calib_sampling_size_lst]
        if self.calib_dataloader:
            self.calib_iter = [math.ceil(int(x) / self.calib_dataloader.batch_size) \
                               for x in calib_sampling_size_lst]
        else:
            self.calib_iter = 1
        # create tuning space
        adaptor_cap = {
            'calib': {'calib_sampling_size': calib_sampling_size_lst},
            'op': self.capability['opwise']
        }
        self.tuning_space = TuningSpace(adaptor_cap, conf=config, framework=self.framework)

    def setup_resume(self, resume):
        """Resume the best quantized model from tuning history.

        Args:
            resume: The dict containing resume information.
        """
        self.__dict__.update(resume)
        for history in self.tuning_history:
            if self._same_conf(history['cfg'], self.conf):
                self.__dict__.update({k: v for k, v in history.items() \
                                        if k not in ['version', 'history']})
                logger.info("Start to resume tuning process.")
                # resume the best tuning model if needed
                try:
                    index = history['id'] - 1
                    resume_tuning_cfg = history['history'][index]['tune_cfg']
                    self.best_qmodel = self.adaptor.quantize(resume_tuning_cfg,
                                                                self.model,
                                                                self.calib_dataloader,
                                                                self.q_func)
                except:
                    logger.debug("Can not resume the best quantize model from history.")

                break

    def set_q_func(self):
        """Set the training function for quantization aware training."""
        if self.config.approach == 'quant_aware_training':
            assert self.q_func != None, "Please set train func for quantization aware training"

    def _create_path(self, custom_path, filename):
        new_path = os.path.join(os.path.abspath(os.path.expanduser(custom_path)),filename)
        path = Path(os.path.dirname(new_path))
        path.mkdir(exist_ok=True, parents=True)
        return new_path

    def _set_framework_info(self, q_dataloader, q_func=None):
        framework_specific_info = {'device': getattr(self.config, 'device', None),
                                   'approach': getattr(self.config, 'approach', None),
                                   'random_seed': options.random_seed,
                                   'performance_only': self._not_tuning}
        framework = self.config.framework.lower()
        framework_specific_info.update({'backend': self.config.backend})
        framework_specific_info.update({'format': getattr(self.config, 'quant_format', None)})
        framework_specific_info.update({'domain': getattr(self.config, 'domain', None)})

        self.mixed_precision_mode = isinstance(self.config, MixedPrecisionConfig)

        if 'tensorflow' in framework:
            framework_specific_info.update(
                {"inputs": self.config.inputs,
                 "outputs": self.config.outputs,
                 'workspace_path': options.workspace,
                 'recipes': self.config.recipes,
                 'use_bf16': self.config.use_bf16 if self.config.use_bf16 is not None else False})
            for item in ['scale_propagation_max_pooling', 'scale_propagation_concat']:
                if framework_specific_info['recipes'] and item not in framework_specific_info['recipes']:
                    framework_specific_info['recipes'].update({item: True})
            if self.config.backend == 'itex':
                framework = 'tensorflow_itex'
        if 'keras' in framework:
            framework_specific_info.update({
                 'workspace_path': options.workspace, })
        if framework == 'mxnet':
            framework_specific_info.update({"q_dataloader": q_dataloader})
        if 'onnx' in framework.lower():
            if self.mixed_precision_mode:
                framework_specific_info.update({"approach": "post_training_dynamic_quant"})
            framework_specific_info.update({"deploy_path": os.path.dirname(self.deploy_path)})
            framework_specific_info.update({'workspace_path': options.workspace})
            framework_specific_info.update({'recipes': self.config.recipes})
            framework_specific_info.update({'reduce_range': self.config.reduce_range})
            framework_specific_info.update({'recipes': self.config.recipes})
            if framework_specific_info['backend'] in ['onnxrt_trt_ep', 'onnxrt_cuda_ep'] and \
                'gpu' not in framework_specific_info['device']:
                logger.warning('Please set device to gpu during using backend {}.'.format(self.config.backend))
                sys.exit(0)
            if framework.lower() == 'onnxrt_qdq' or \
                framework_specific_info['backend'] == 'onnxrt_trt_ep':
                framework_specific_info.update({'format': 'QDQ'})
                framework = 'onnxrt_qdq'
        if framework == 'pytorch_ipex' or framework == 'pytorch' or framework == 'pytorch_fx':
            if self.config.backend == 'ipex':
                framework = 'pytorch_ipex'
            elif self.config.backend == 'default':
                framework = 'pytorch_fx'
            if self.mixed_precision_mode:
                framework_specific_info.update({"approach": "post_training_dynamic_quant"})
            framework_specific_info.update({'recipes': self.config.recipes})
            framework_specific_info.update({"q_dataloader": q_dataloader})
            framework_specific_info.update({"use_bf16": self.config.use_bf16 \
                            if self.config.use_bf16 is not None else True})
            framework_specific_info.update(
                {"workspace_path": os.path.dirname(self.deploy_path)})
            if self.config.op_name_dict is not None \
               and 'default_qconfig' in self.config.op_name_dict:
                framework_specific_info.update(
                    {"default_qconfig": self.config.op_name_dict['default_qconfig']})
            framework_specific_info.update({"q_func": q_func})
            framework_specific_info.update({"example_inputs": self.config.example_inputs})
        return framework, framework_specific_info

    def _set_objectives(self):
        # set objectives
        self.higher_is_better = bool(self.config.accuracy_criterion.higher_is_better)
        obj_higher_is_better = None
        obj_weight = None
        obj = self.config.tuning_criterion.objective
        use_multi_objs = isinstance(obj, dict)
        self.use_multi_objective = False
        if use_multi_objs:
            obj_higher_is_better = obj.get('higher_is_better', None)
            obj_weight = obj.get('weight', None)
            obj_lst = obj.get('objective', [])
            objectives = [i.lower() for i in obj_lst]
            self.use_multi_objective = True
        else:
            objectives = [obj.lower()]

        # set metric
        self.metric_name = ['Accuracy']
        self.metric_criterion = [self.higher_is_better]
        self.metric_weight = None
        use_multi_metrics = False
        if self.eval_metric:
            # metric name
            # 'weight','higher_is_better', 'metric1', 'metric2', ...
            if len(self.eval_metric.keys()) >= 4:
                self.metric_name = self.eval_metric.keys() - {'weight','higher_is_better'}
                use_multi_metrics = True
            metric_higher_is_better = self.eval_metric.get('higher_is_better', None)
            # metric criterion
            if use_multi_metrics:
                if metric_higher_is_better is not None:
                    self.metric_criterion = [metric_higher_is_better] * len(self.metric_name)
                else:
                    self.metric_criterion = [True] * len(self.metric_name)
            # metric weight
            self.metric_weight = self.eval_metric.get('weight', None)

        accuracy_criterion = {'relative': 0.01, 'higher_is_better': True}
        accuracy_criterion_conf = self.config.accuracy_criterion
        accuracy_criterion[accuracy_criterion_conf.criterion] = accuracy_criterion_conf.tolerable_loss
        accuracy_criterion['higher_is_better'] = accuracy_criterion_conf.higher_is_better
        self.objectives = MultiObjective(objectives=objectives,
                                         accuracy_criterion=accuracy_criterion,
                                         metric_criterion=self.metric_criterion,
                                         metric_weight=self.metric_weight,
                                         obj_criterion=obj_higher_is_better,
                                         obj_weight=obj_weight)

    def _same_conf(self, src_conf, dst_conf):
        """Check if the two configs are the same."""
        from ..utils.utility import compare_objects
        return compare_objects(src_conf, dst_conf, {'_options', '_tuning', '_accuracy'})

    def update_best_op_tuning_cfg(self, op_tuning_cfg):
        """Track and update the best tuning config with correspondence accuracy result.

        Args:
            op_tuning_cfg: The tuning config.

        Returns:
            The current best tuning results and corresponding configurations.
        """
        acc, _ = self.last_tune_result
        if self.cur_best_tuning_cfg is None:
            self.cur_best_tuning_cfg = copy.deepcopy(op_tuning_cfg)
        if not isinstance(acc, list) and ((self.higher_is_better and acc >= self.cur_best_acc) \
            or (not self.higher_is_better and acc <= self.cur_best_acc)):
            self.cur_best_acc = acc
            self.cur_best_tuning_cfg = copy.deepcopy(op_tuning_cfg)
        elif len(self.metric_name) > 1 and self.metric_weight is not None:
            acc = np.mean(np.array(acc) * self.metric_weight)
            if (self.higher_is_better and acc >= self.cur_best_acc) or \
                (not self.higher_is_better and acc <= self.cur_best_acc):
                self.cur_best_acc = acc
                self.cur_best_tuning_cfg = copy.deepcopy(op_tuning_cfg)
        elif len(self.metric_name) > 1 and self.metric_weight is None:
            if all([acc_i >= best_i if higher_is_better else acc_i <= best_i for \
                acc_i, best_i, higher_is_better in \
                zip(acc, self.cur_best_acc, self.metric_criterion)]):
                self.cur_best_acc = acc
                self.cur_best_tuning_cfg = copy.deepcopy(op_tuning_cfg)
        logger.debug(f"Best acc is {self.cur_best_acc}.")
        return self.cur_best_acc, self.cur_best_tuning_cfg

    def deploy_config(self):
        """Save the configuration locally for deployment."""
        self.deploy_cfg = OrderedDict()
        model_cfg = dict()
        model_cfg['inputs'] = self.config.inputs
        model_cfg['outputs'] = self.config.outputs
        model_cfg['backend'] = self.config.backend
        model_cfg['quant_format'] = self.config.quant_format
        model_cfg['domain'] = self.config.domain
        model_cfg['backend'] = self.config.backend
        self.deploy_cfg['model'] = model_cfg
        self.deploy_cfg['device'] = self.config.device

        def setup_yaml():
            represent_dict_order = lambda self, \
                data: self.represent_mapping('tag:yaml.org,2002:map', data.items())
            yaml.add_representer(OrderedDict, represent_dict_order)
            yaml.add_representer(DotDict, represent_dict_order)
        setup_yaml()
        with open(self.deploy_path, 'w+') as f:
            yaml.dump(self.deploy_cfg, f)
            logger.info("Save deploy yaml to {}".format(self.deploy_path))

    @property
    def evaluation_result(self):
        """Evaluate the given model.

        Returns:
            The objective value evaluated.
        """
        return self._evaluate(self.model)

    def _evaluate(self, model):
        """Interface of evaluating model.

        Args:
            model (object): The model to be evaluated.

        Returns:
            Objective: The objective value evaluated.
        """
        if self.eval_func:
            if options.tensorboard:
                # Pytorch can insert observer to model in this hook.
                # Tensorflow don't support this mode for now
                model = self.adaptor._pre_eval_hook(model)
            val = self.objectives.evaluate(
                self.eval_func, model if self.framework == "pytorch_ipex" else model.model
            )
            if options.tensorboard:
                # post_eval_hook to deal the tensor
                self.adaptor._post_eval_hook(model, accuracy=val[0])
        else:
            assert not self._not_tuning, "Please set eval_dataloader and eval_metric for create eval_func"

            postprocess_cfg = None
            metric_cfg = self.eval_metric
            iteration = -1
            eval_func = create_eval_func(self.framework,
                self.eval_dataloader,
                self.adaptor,
                metric_cfg,
                postprocess_cfg,
                iteration,
                tensorboard = options.tensorboard,
                fp32_baseline = self.baseline == None)

            if getattr(self.eval_dataloader, 'distributed', False):
                if 'tensorflow' in self.framework:
                    import horovod.tensorflow as hvd
                elif self.framework in ['pytorch_ipex','pytorch','pytorch_fx']:
                    import horovod.torch as hvd
                else:
                    raise NotImplementedError("Currently only TensorFlow and PyTorch "
                                              "support distributed inference in PTQ.")
                hvd.init()
                try:
                    len_dataloader = len(self.eval_dataloader)
                except:
                    logger.info("The length of the distributed dataloader is unknown."
                                "When the iteration of evaluation dataloader in each "
                                "process is inconsistent, an error may occur.")
                else:
                    list_len_dataloader = hvd.allgather_object(len_dataloader)
                    if hvd.rank() == 0:
                        for i in range(len(list_len_dataloader)-1):
                            if list_len_dataloader[i] != list_len_dataloader[i+1]:
                                raise AttributeError("The evaluation dataloader's iteration is"
                                                     "different between processes, please reset "
                                                     "dataloader's batch_size.")
            val = self.objectives.evaluate(eval_func, model)
        if isinstance(val[0], list):
            assert all([np.isscalar(i) for i in val[0]]), \
                "The eval_func should return a scalar or list of scalar, " \
                "but not {}!".format(str([type(i) for i in val[0]]))
        else:
            assert np.isscalar(val[0]), \
                "The eval_func should return a scalar or list of scalar, " \
                "but not {}!".format(str(type(val[0])))

        return val

    def __getstate__(self):
        """Magic method for pickle saving.

        Returns:
            dict: Saved dict for resuming
        """
        return {'tuning_history': self.tuning_history}

    def __setstate__(self, d):
        """Magic method for pickle loading.

        Args:
            d (dict): The dict to load.
        """
        self.__dict__.update(d)

    def stop(self, timeout, trials_count):
        """Check if need to stop traverse.

        Check if need to stop traversing the tuning space, either accuracy goal is met or timeout is reach.

        Returns:
            bool: True if need stop, otherwise False
        """
        need_stop = False
        if self._not_tuning or \
            self.objectives.compare(self.best_tune_result, self.baseline):
            self.best_tune_result = self.last_tune_result
            self.best_qmodel = self.last_qmodel
            self.best_tuning_cfg = copy.deepcopy(self.last_tune_cfg)
            logger.debug(f"*** Update the best qmodel with the result {self.best_tune_result}")
            if self.metric_met_point == 0:
                self.metric_met_point = self.tuning_times

        # track the model with highest acc
        if self.best_tune_result and self.last_tune_result: # (acc, [perf])
            if self.re_quant and self.objectives.accuracy_meets():
                self.best_tune_result = self.last_tune_result
                self.best_qmodel = self.last_qmodel
                self.best_tuning_cfg = copy.deepcopy(self.last_tune_cfg)
                logger.debug(f"*** Update the best qmodel with the result {self.best_tune_result}.")
            else:
                logger.debug(f"*** Accuracy not meets the requirements, do not update the best qmodel.")

        if self.last_tune_result:
            last_tune = self.last_tune_result[0] if \
                isinstance(self.last_tune_result[0], list) else [self.last_tune_result[0]]

            for name, data in zip(self.metric_name, last_tune):
                if len(self.tune_data[name]) == 1:
                    self.tune_data[name].append(data)
                else:
                    self.tune_data[name][1] = data

            if self.metric_weight and len(last_tune) > 1:
                weighted_acc = np.mean(np.array(last_tune) * self.metric_weight)

                if len(self.tune_data['Weighted accuracy']) == 1:
                    self.tune_data['Weighted accuracy'].append(weighted_acc)
                else:
                    self.tune_data['Weighted accuracy'][1] = weighted_acc

                last_tune = [weighted_acc]

            last_tune_msg = '[Accuracy (int8|fp32):' + \
                ''.join([' {:.4f}|{:.4f}'.format(last, base) for last, base in \
                zip(last_tune, self.tune_data['baseline'])]) + \
                ''.join([', {} (int8|fp32): {:.4f}|{:.4f}'.format( \
                x, y, z) for x, y, z in zip( \
                self.objectives.representation, self.last_tune_result[1], self.baseline[1]) \
                if x != 'Accuracy']) + ']'
        else: # pragma: no cover
            last_tune_msg = 'n/a'
            for name in self.tune_data.keys() - {'baseline'}:
                if len(self.tune_data[name]) == 1:
                    self.tune_data[name].append('n/a')
                else:
                    self.tune_data[name][1] = 'n/a'

        if self.best_tune_result:
            best_tune = self.best_tune_result[0] if isinstance(self.best_tune_result[0], list) \
                        else [self.best_tune_result[0]]

            for name, data in zip(self.metric_name, best_tune):
                if len(self.tune_data[name]) == 2:
                    self.tune_data[name].append(data)
                else:
                    self.tune_data[name][2] = data

            if self.metric_weight and len(best_tune) > 1:
                weighted_acc = np.mean(np.array(best_tune) * self.metric_weight)

                if len(self.tune_data['Weighted accuracy']) == 2:
                    self.tune_data['Weighted accuracy'].append(weighted_acc)
                else: # pragma: no cover
                    self.tune_data['Weighted accuracy'][2] = weighted_acc

                best_tune = [weighted_acc]

            best_tune_msg = '[Accuracy:' + ''.join([' {:.4f}'.format(best) \
                for best in best_tune]) + ''.join([', {}: {:.4f}'.format(x,y) \
                for x,y in zip(self.objectives.representation, \
                self.best_tune_result[1]) if x != 'Accuracy']) + ']'

        else:
            best_tune_msg = 'n/a'
            for name in self.tune_data.keys() - {'baseline'}:
                if len(self.tune_data[name]) == 2:
                    self.tune_data[name].append('n/a')
                else:
                    self.tune_data[name][2] = 'n/a'

        logger.info("Tune {} result is: {}, Best tune result is: {}".format(self.trials_count,
                                                                            last_tune_msg,
                                                                            best_tune_msg))
        output_data = [[info_type,
            '{:.4f} '.format(self.tune_data[info_type][0]) if \
            not isinstance(self.tune_data[info_type][0], str) else self.tune_data[info_type][0],
            '{:.4f} '.format(self.tune_data[info_type][1]) if \
            not isinstance(self.tune_data[info_type][1], str) else self.tune_data[info_type][1],
            '{:.4f} '.format(self.tune_data[info_type][2]) if \
            not isinstance(self.tune_data[info_type][2], str) else self.tune_data[info_type][2]] \
            for info_type in self.tune_data.keys() if info_type != 'baseline']

        output_data.extend([[obj,
            '{:.4f} '.format(self.baseline[1][i]) if self.baseline else 'n/a',
            '{:.4f} '.format(self.last_tune_result[1][i]) if self.last_tune_result else 'n/a',
            '{:.4f} '.format(self.best_tune_result[1][i]) if self.best_tune_result else 'n/a'] \
            for i, obj in enumerate(self.objectives.representation)])
        self.tuning_result_data = output_data
        Statistics(output_data,
                   header='Tune Result Statistics',
                   field_names=['Info Type', 'Baseline', 'Tune {} result'.format(self.trials_count), \
                                                                'Best tune result']).print_stat()


        if self._not_tuning:
            need_stop = True
        elif timeout == 0 and self.best_tune_result:
            need_stop = True
        elif self.trials_count >= self.config.tuning_criterion.max_trials:
            need_stop = True
        else:
            need_stop = False

        return need_stop

    def _save(self):
        """Save current tuning state to snapshot for resuming."""
        logger.info("Save tuning history to {}.".format(self.history_path))
        with fault_tolerant_file(self.history_path) as f:
            pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

    def _find_tuning_history(self, tune_cfg):
        """Check if the specified tune_cfg is evaluated or not on same config.

        Args:
            tune_cfg (dict): The tune_cfg to check if evaluated before.

        Returns:
            tuning_history or None: The tuning history containing evaluated tune_cfg.
        """
        for tuning_history in self.tuning_history:
            # only check if a tune_cfg is evaluated under same config, excluding
            # some fields in tuning section of config, such as tensorboard, snapshot, resume.
            if self._same_conf(tuning_history['cfg'], self.conf):
                for history in tuning_history['history']:
                    if history and history['tune_cfg'] == tune_cfg:
                        return tuning_history

        return None

    def _find_history(self, tune_cfg):
        """Check if the specified tune_cfg is evaluated or not on same config.

        Returns:
            history or None: The history containing evaluated tune_cfg.
        """
        for tuning_history in self.tuning_history:
            # only check if a tune_cfg is evaluated under same config, excluding
            # some fields in tuning section of config, such as tensorboard, snapshot, resume.
            if self._same_conf(tuning_history['cfg'], self.conf):
                for history in tuning_history['history']:
                    if history and history['tune_cfg'] == tune_cfg:
                        return history
        return None

    def _find_self_tuning_history(self):
        """Find self history dict.

        Returns:
            history or None: The history for self.
        """
        for tuning_history in self.tuning_history:
            # only check if a tune_cfg is evaluated under same config, excluding
            # some fields in tuning section of config, such as tensorboard, snapshot, resume.
            if self._same_conf(tuning_history['cfg'], self.conf):
                return tuning_history

        return None

    def _add_tuning_history(self, tune_cfg=None, tune_result=None, **kwargs):
        """Add tuning config to tuining history.

        The tuning history ever made, structured like below:
        [
          {
            'version': __version__,
            'cfg': cfg1,
            'framework': tensorflow
            'baseline': baseline1,
            'last_tune_result': last_tune_result1,
            'best_tune_result': best_tune_result1,
            'history': [
                         # tuning history under same config
                         {'tune_cfg': tune_cfg1, 'tune_result': \
                                      tune_result1, 'q_config': q_config1, ...},

                          ...,
                       ],
            # new fields added by subclass for resuming
            ...,
          },
          # tuning history under different configs
          ...,
        ]

        Note this record is added under same config.
        """
        found = False
        d = {'tune_cfg': tune_cfg, 'tune_result': tune_result}
        for tuning_history in self.tuning_history:
            if self._same_conf(tuning_history['cfg'], self.conf):
                d.update(kwargs)
                tuning_history['history'].append(d)
                tuning_history['last_tune_result'] = self.last_tune_result
                tuning_history['best_tune_result'] = self.best_tune_result
                tuning_history['cfg'] = self.conf
                found = True
                break

        if not found:
            tuning_history = {}
            tuning_history['version']  = __version__
            tuning_history['cfg']     = self.conf
            tuning_history['baseline'] = self.baseline
            tuning_history['last_tune_result'] = self.last_tune_result
            tuning_history['best_tune_result'] = self.best_tune_result
            tuning_history['history']  = []
            if tune_cfg and tune_result:
                d.update(kwargs)
                tuning_history['history'].append(d)
            self.tuning_history.append(tuning_history)

        self._save()

    def _collect_ops_by_quant_mode(self, tune_cfg, quant_mode):
        ops_lst = []
        for op_info, op_config in tune_cfg.items():
            if isinstance(op_config, OpTuningConfig) and quant_mode in op_config.op_quant_mode:
                ops_lst.append(op_info)
        return ops_lst

    def _diagnosis(self):
        import logging
        logger = logging.getLogger("neural_compressor")
        iteration_list = [1]
        inspect_type = 'all'
        save_to_disk = True
        save_path = './nc_workspace/inspect_saved/'
        inspect_node_lst, updated_cfg = self.adaptor.diagnosis_helper(self._fp32_model,
                                                                      self.last_qmodel,
                                                                      self.tune_cfg,
                                                                      save_path = save_path)
        op_list = []
        if not op_list:
            op_list = list(inspect_node_lst)
        else:
            op_list = list(set(op_list).intersection(inspect_node_lst))

        logger.debug(f'*** Start to inspect tensor :{op_list} in  fp32 model.')
        self.adaptor.inspect_tensor(self._fp32_model,
                                    dataloader=self.calib_dataloader,
                                    op_list=op_list,
                                    iteration_list=iteration_list,
                                    inspect_type=inspect_type,
                                    save_to_disk=save_to_disk,
                                    save_path= save_path + '/fp32/',
                                    quantization_cfg=updated_cfg)

        logger.debug(f'*** Start to inspect tensor :{op_list} in  quantized model.')
        self.adaptor.inspect_tensor(self.last_qmodel,
                                    dataloader=self.calib_dataloader,
                                    op_list=op_list,
                                    iteration_list=iteration_list,
                                    inspect_type=inspect_type,
                                    save_to_disk=save_to_disk,
                                    save_path= save_path + '/quan/',
                                    quantization_cfg=updated_cfg)
