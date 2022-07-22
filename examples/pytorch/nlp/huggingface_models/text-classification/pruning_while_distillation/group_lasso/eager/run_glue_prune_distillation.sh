python run_glue_no_trainer_sparse.py --model_name_or_path ./bert-mini --task_name sst2 --max_length 128 --per_device_train_batch_size 16 --learning_rate 2e-5 --num_train_epochs 20 --output_dir result/ --do_prune --do_distillation 2>&1 | tee result/code_test.log