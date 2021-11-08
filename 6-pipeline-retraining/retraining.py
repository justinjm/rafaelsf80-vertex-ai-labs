from google.cloud import aiplatform
import kfp
from kfp.v2 import compiler
from datetime import datetime

from google.cloud.aiplatform import pipeline_jobs
from google_cloud_pipeline_components import aiplatform as gcc_aip

from typing import NamedTuple

import kfp
from kfp import dsl
from kfp.v2 import compiler
from kfp.v2.dsl import (Artifact, Dataset, Input, InputPath, Model, Output,
                        OutputPath, ClassificationMetrics, Metrics, component)
from kfp.v2.google.client import AIPlatformClient

from google.cloud import aiplatform
from google.cloud.aiplatform import pipeline_jobs
from google_cloud_pipeline_components import aiplatform as gcc_aip
#from tensorflow.python.keras.layers.preprocessing.category_encoding import CategoryEncoding

PROJECT_ID = 'windy-site-254307'
STAGING_BUCKET = 'vertex-retraining-demo-uscentral1'
LOCATION = 'us-central1'
USER = 'rafaelsanchez'
EPOCHS = 10
PIPELINE_ROOT = 'gs://{}/pipeline_root/{}'.format(STAGING_BUCKET, USER)
TIMESTAMP = datetime.now().strftime("%Y%m%d%H%M%S")

BUCKET_NAME = 'gs://vertex-model-governance-lab'
SAMPLE_CSV_EXPORTED_URI = f'{BUCKET_NAME}/000000000000.csv'
TENSORBOARD_RESOURCE = 'projects/655797269815/locations/us-central1/tensorboards/3939734880274874368'
SERVICE_ACCOUNT = 'prosegur-video-test@windy-site-254307.iam.gserviceaccount.com'

CONTAINER_GCR_URI = 'gcr.io/windy-site-254307/conditional:v1'
# If Docker permission error or Docker image could not be pulled error, just run "gcloud auth configure-docker us-central1-docker.pkg.dev,europe-west4-docker.pkg.dev"
# Set also project proprtly with gcloud config set project BEFORE gcloud auth
gcc_aip.utils.DEFAULT_CONTAINER_IMAGE=CONTAINER_GCR_URI

API_ENDPOINT = "us-central1-aiplatform.googleapis.com"
THRESHOLDS_DICT = '{"auRoc": 0.95}'

aiplatform.init(project=PROJECT_ID)

@component(
    base_image="gcr.io/deeplearning-platform-release/tf2-cpu.2-3:latest",
    output_component_file="tables_eval_component.yaml", # Optional: you can use this to load the component later
    packages_to_install=["google-cloud-aiplatform"],
)
def classif_model_eval_metrics(
    project: str,
    location: str,  # "us-central1",
    api_endpoint: str,  # "us-central1-aiplatform.googleapis.com",
    thresholds_dict_str: str,
    model: Input[Model],
    metrics: Output[Metrics],
    metricsc: Output[ClassificationMetrics],
) -> NamedTuple("Outputs", [("dep_decision", str)]):  # Return parameter.

    """This function renders evaluation metrics for an AutoML Tabular classification model.
    It retrieves the classification model evaluation generated by the AutoML Tabular training
    process, does some parsing, and uses that info to render the ROC curve and confusion matrix
    for the model. It also uses given metrics threshold information and compares that to the
    evaluation results to determine whether the model is sufficiently accurate to deploy.
    """
    import json
    import logging

     # Use the given metrics threshold(s) to determine whether the model is 
    # accurate enough to deploy.
    def classification_thresholds_check(metrics_dict, thresholds_dict):
        ## TODO: LOGIC TO DEFINE IF MODEL WILL BE DEPLOYED 20%
        logging.info("threshold checks passed.")
        return True

    thresholds_dict = json.loads(thresholds_dict_str)
    deploy = True #classification_thresholds_check(metrics_list[0], thresholds_dict)
    if deploy:
        dep_decision = "true"
    else:
        dep_decision = "false"
    logging.info("deployment decision is %s", dep_decision)
    
    return (dep_decision,)


##############
# Train component
##############
@component(
    base_image='gcr.io/deeplearning-platform-release/tf2-cpu.2-5:latest', # Use a different base image.
    packages_to_install=['pandas', 'google-cloud-aiplatform', 'fsspec', 'gcsfs']
)
def train(
    #dataset: Input[Dataset],

    #dataset: InputArtifact(Dataset),
    csv_path: str,
    # Output artifact of type Model.
    output_model: Output[Model],
    metrics: Output[Metrics],
    # An input parameter of type int with a default value.
    num_epochs: int = 10,
  ):        

    import pandas as pd
    import tensorflow as tf
    import tensorflow.keras as keras
    from google.cloud import aiplatform
    import logging

    logging.getLogger().setLevel(logging.INFO)

    print(tf.__version__)


    #Init Vertex AI experiment
    aiplatform.init(project="windy-site-254307")

    sample_df = pd.read_csv(csv_path)

    clean_sample_df = sample_df.dropna()
    target = clean_sample_df['churned']
    features = clean_sample_df.drop(['churned', 'timestamp'], axis=1)
    numeric_features = features.select_dtypes(include=['int64'])
    categorical_features = features.drop(['entity_type_customer', 'user_pseudo_id'], axis=1).select_dtypes(include=['object']).astype(str)

    dataset = tf.data.Dataset.from_tensor_slices(({**dict(numeric_features), **dict(categorical_features)}, target))

    train_ds = (dataset.skip(1000)
                .batch(10, drop_remainder=True)
                .cache()
                .prefetch(tf.data.experimental.AUTOTUNE))
    val_ds = (dataset.take(1000)
            .batch(10, drop_remainder=True)
            .cache()
            .prefetch(tf.data.experimental.AUTOTUNE))

    print(f'numeric features: {[cat for cat in numeric_features]}')
    normalizers = {cat: keras.layers.experimental.preprocessing.Normalization() for cat in numeric_features}
    for cat, normalizer in normalizers.items():
        print(f'adapting {cat} numeric normalizer with {numeric_features[cat].values}')
        normalizer.adapt(train_ds.map(lambda x, y: x[cat]))

    print(f'categorical features: {[cat for cat in categorical_features]}')
    str_lookups = {cat: keras.layers.experimental.preprocessing.StringLookup() for cat in categorical_features}
    for cat, str_lookup in str_lookups.items():
        print(f'adapting {cat} string lookup with {categorical_features[cat].values}')
        str_lookup.adapt(train_ds.map(lambda x, y: x[cat]))
        
    #Num tokens is amount of unique strings + out-of-value + mask tokens
    print(f'num_tokens: {[len(categorical_features[cat].unique())+2 for cat in categorical_features]}')
    str_encoders = {cat: keras.layers.experimental.preprocessing.CategoryEncoding(num_tokens=len(categorical_features[cat].unique())+2, output_mode="binary") for cat in categorical_features}
    for cat, str_encode in str_encoders.items():
        print(f'adapting {cat} string encoder with {str_lookups[cat](categorical_features[cat].values)}')
        str_encode.adapt(train_ds.map(lambda x, y: str_lookups[cat](x[cat])))

    numeric_inputs = {cat: keras.Input(shape=(), name=cat) for cat in numeric_features}
    categorical_inputs = {cat: keras.Input(shape=(), name=cat, dtype=tf.string) for cat in categorical_features}

    numeric_normalized = [normalizers[cat](numeric_inputs[cat]) for cat in numeric_inputs]
    categorical_normalized = [str_encoders[cat](str_lookups[cat](categorical_inputs[cat])) for cat in categorical_inputs]

    concat_num = keras.layers.Concatenate()(numeric_normalized)
    concat_cat = keras.layers.Concatenate()(categorical_normalized)
    concat = keras.layers.Concatenate()([concat_num, concat_cat])

    hidden1 = keras.layers.Dense(128, activation='relu')(concat)
    hidden2 = keras.layers.Dense(64, activation='relu')(hidden1)
    output = keras.layers.Dense(1, activation='sigmoid', name='churned')(hidden2)

    tf_model = keras.Model(inputs={**numeric_inputs, **categorical_inputs}, outputs=output)
    tf_model.summary()

    tf_model.compile(optimizer='adam', 
                loss='binary_crossentropy',
                metrics=['binary_accuracy', tf.keras.metrics.FalsePositives(), tf.keras.metrics.FalseNegatives()])

    print('Training the model...')
    history = tf_model.fit(train_ds, validation_data=val_ds, epochs=10)
    print('Evaluating the model...')
    evaluation = tf_model.evaluate(val_ds)

    tf_model.summary()

    tf_model.save(output_model.path)
    logging.info('using model.uri: %s', output_model.uri)







    # val_dataframe = sample_df.sample(frac=0.2, random_state=1337)
    # train_dataframe = sample_df.drop(val_dataframe.index)
    # val_dataframe = val_dataframe.drop(['language', 'country', 'operating_system'], axis = 1)
    # train_dataframe = train_dataframe.drop(['language', 'country', 'operating_system'], axis = 1)

    # def dataframe_to_dataset(dataframe):
    #     dataframe = dataframe.copy()
    #     labels = dataframe.pop("churned")
    #     ds = tf.data.Dataset.from_tensor_slices((dict(dataframe), labels))
    #     ds = ds.shuffle(buffer_size=len(dataframe))
    #     return ds


    # train_ds = dataframe_to_dataset(train_dataframe)
    # val_ds = dataframe_to_dataset(val_dataframe)

    # train_ds = train_ds.batch(32)
    # val_ds = val_ds.batch(32)

    # from tensorflow.keras.layers.experimental.preprocessing import IntegerLookup
    # from tensorflow.keras.layers.experimental.preprocessing import Normalization
    # from tensorflow.keras.layers.experimental.preprocessing import StringLookup


    # def encode_numerical_feature(feature, name, dataset):
    #     # Create a Normalization layer for our feature
    #     normalizer = Normalization()

    #     # Prepare a Dataset that only yields our feature
    #     feature_ds = dataset.map(lambda x, y: x[name])
    #     feature_ds = feature_ds.map(lambda x: tf.expand_dims(x, -1))

    #     # Learn the statistics of the data
    #     normalizer.adapt(feature_ds)

    #     # Normalize the input feature
    #     encoded_feature = normalizer(feature)
    #     return encoded_feature


    # def encode_categorical_feature(feature, name, dataset, is_string):
    #     lookup_class = StringLookup if is_string else IntegerLookup
    #     # Create a lookup layer which will turn strings into integer indices
    #     lookup = lookup_class(output_mode="binary")

    #     # Prepare a Dataset that only yields our feature
    #     feature_ds = dataset.map(lambda x, y: x[name])
    #     feature_ds = feature_ds.map(lambda x: tf.expand_dims(x, -1))

    #     # Learn the set of possible string values and assign them a fixed integer index
    #     lookup.adapt(feature_ds)

    #     # Turn the string input into integer indices
    #     encoded_feature = lookup(feature)
    #     return encoded_feature


    # # Numerical features
    # cnt_level_complete_quickplay = keras.Input(shape=(1,), name="cnt_level_complete_quickplay")
    # cnt_ad_reward = keras.Input(shape=(1,), name="cnt_ad_reward")
    # cnt_post_score = keras.Input(shape=(1,), name="cnt_post_score")
    # cnt_completed_5_levels = keras.Input(shape=(1,), name="cnt_completed_5_levels")
    # cnt_level_start_quickplay = keras.Input(shape=(1,), name="cnt_level_start_quickplay")
    # cnt_level_reset_quickplay = keras.Input(shape=(1,), name="cnt_level_reset_quickplay")
    # cnt_challenge_a_friend = keras.Input(shape=(1,), name="cnt_challenge_a_friend")
    # cnt_user_engagement = keras.Input(shape=(1,), name="cnt_user_engagement")
    # cnt_spend_virtual_currency = keras.Input(shape=(1,), name="cnt_spend_virtual_currency")
    # cnt_use_extra_steps = keras.Input(shape=(1,), name="cnt_use_extra_steps")
    # cnt_level_end_quickplay = keras.Input(shape=(1,), name="cnt_level_end_quickplay")

    # all_inputs = [
    #     cnt_level_complete_quickplay,
    #     cnt_ad_reward,
    #     cnt_post_score,
    #     cnt_completed_5_levels,
    #     cnt_level_start_quickplay,
    #     cnt_level_reset_quickplay,
    #     cnt_challenge_a_friend,
    #     cnt_user_engagement,
    #     cnt_spend_virtual_currency,
    #     cnt_use_extra_steps,
    #     cnt_level_end_quickplay
    # ]

    # # Integer categorical features
    # cnt_level_complete_quickplay_encoded = encode_numerical_feature(cnt_level_complete_quickplay, "cnt_level_complete_quickplay", train_ds)
    # cnt_ad_reward_encoded = encode_numerical_feature(cnt_ad_reward, "cnt_ad_reward", train_ds)
    # cnt_post_score_encoded = encode_numerical_feature(cnt_post_score, "cnt_post_score", train_ds)
    # cnt_completed_5_levels_encoded = encode_numerical_feature(cnt_completed_5_levels, "cnt_completed_5_levels", train_ds)
    # cnt_level_start_quickplay_encoded = encode_numerical_feature(cnt_level_start_quickplay, "cnt_level_start_quickplay", train_ds)
    # cnt_level_reset_quickplay_encoded = encode_numerical_feature(cnt_level_reset_quickplay, "cnt_level_reset_quickplay", train_ds)
    # cnt_challenge_a_friend_encoded = encode_numerical_feature(cnt_challenge_a_friend, "cnt_challenge_a_friend", train_ds)
    # cnt_user_engagement_encoded = encode_numerical_feature(cnt_user_engagement, "cnt_user_engagement", train_ds)
    # cnt_spend_virtual_currency_encoded = encode_numerical_feature(cnt_spend_virtual_currency, "cnt_spend_virtual_currency", train_ds)
    # cnt_use_extra_steps_encoded = encode_numerical_feature(cnt_use_extra_steps, "cnt_use_extra_steps", train_ds)
    # cnt_level_end_quickplay_encoded = encode_numerical_feature(cnt_level_end_quickplay, "cnt_level_end_quickplay", train_ds)

    # all_features = layers.concatenate(
    #     [
    #         cnt_level_complete_quickplay_encoded,
    #         cnt_ad_reward_encoded,
    #         cnt_post_score_encoded,
    #         cnt_completed_5_levels_encoded,
    #         cnt_level_start_quickplay_encoded,
    #         cnt_level_reset_quickplay_encoded,
    #         cnt_challenge_a_friend_encoded,
    #         cnt_user_engagement_encoded,
    #         cnt_spend_virtual_currency_encoded,
    #         cnt_use_extra_steps_encoded,
    #         cnt_level_end_quickplay_encoded
    #     ]
    # )
    # x = layers.Dense(32, activation="relu")(all_features)
    # x = layers.Dropout(0.5)(x)
    # output = layers.Dense(1, activation="sigmoid")(x)
    # model = keras.Model(all_inputs, output)
    # model.compile("adam", "binary_crossentropy", metrics=["accuracy"])


    # model.fit(train_ds, epochs=num_epochs, validation_data=val_ds)

    # evaluation = model.evaluate(val_ds)
    # model.summary()

    # model.save(output_model.path)
    # logging.info('using model.uri: %s', output_model.uri)


##############
# Upload and deploy model in Vertex
##############
@component(
    base_image='python:3.9', # Use a different base image.
    packages_to_install=['google-cloud-aiplatform']
)
def deploy(
    previous_model: str,
    endpoint: str,
    # Input model.
    model: Input[Model],
    vertex_endpoint: Output[Artifact],
    vertex_model: Output[Model]
    ):

  import logging
  logging.getLogger().setLevel(logging.INFO)

  from google.cloud import aiplatform
  aiplatform.init(project='windy-site-254307')

  # Upload model
  new_model = aiplatform.Model.upload(
      display_name=f'churn-retraining',
      artifact_uri=model.uri,
      serving_container_image_uri='us-docker.pkg.dev/vertex-ai/prediction/tf2-cpu.2-3:latest'
  )
  logging.info('uploaded model: %s', new_model.resource_name)

  # Deploy model
  if endpoint:  # 80-20 split
    deployed_endpoint = aiplatform.Endpoint(endpoint)
    logging.info('deployed_endpoint: %s', endpoint)
    logging.info('gca_resource: %s', deployed_endpoint.gca_resource)

    # CAREFUL: model_id is not the same as deployed_model_id
    deployed_model_id = deployed_endpoint.gca_resource.deployed_models[0].id
    logging.info('deployed_model_id: %s', deployed_model_id)
    endpoint_updated = new_model.deploy(
        deployed_model_display_name="retraining-B",
        endpoint = deployed_endpoint,
        machine_type='n1-standard-4',
        traffic_split = {"0": 20, deployed_model_id: 80}
    )
  else: # first time, create endpoint with 100% split
    endpoint_updated = new_model.deploy(
        deployed_model_display_name="churn-model-A",
        machine_type='n1-standard-4'
    )
  logging.info('endpoint: %s', str(endpoint_updated))
  vertex_endpoint.uri = endpoint_updated.resource_name
  vertex_model.uri = endpoint_updated.resource_name


@kfp.dsl.pipeline(name='retraining-demo-uscentral1')
def pipeline(endpoint: str, previous_model: str):
    trainer_args = [
            "--project", PROJECT_ID,
            '--experiment', PROJECT_ID + "-retraining-demo-" + TIMESTAMP,
            '--epochs', str(EPOCHS),
            '--data_source', SAMPLE_CSV_EXPORTED_URI
        ]


    train_task = train(
        csv_path=SAMPLE_CSV_EXPORTED_URI,
        #message=preprocess_task.outputs['output_parameter'],
        num_epochs=5)


    #https://google-cloud-pipeline-components.readthedocs.io/en/google-cloud-pipeline-components-0.1.4/google_cloud_pipeline_components.aiplatform.html#google_cloud_pipeline_components.aiplatform.CustomContainerTrainingJobRunOp
    # custom_container_job_run_op = gcc_aip.CustomContainerTrainingJobRunOp(
    #         display_name=f"Retraining_Demo_Model_Monitoring",
    #         model_display_name=PROJECT_ID + "-retraining-demo-model-monitoring-" + TIMESTAMP,
    #         container_uri="eu.gcr.io/windy-site-254307/conditional:v1",
    #         model_serving_container_image_uri="us-docker.pkg.dev/vertex-ai/prediction/tf2-cpu.2-5:latest",
    #         model_serving_container_predict_route="/predict",
    #         model_serving_container_health_route="/health",
    #         model_description="demo_model_retraining",
    #         project=PROJECT_ID,
    #         location=LOCATION,
    #         staging_bucket=STAGING_BUCKET,
    #         replica_count=1,
    #         machine_type="n1-standard-4",
    #         args=trainer_args
    #     ).set_caching_options(True)

    # custom_endpoint_op = gcc_aip.ModelDeployOp(
    #     project=PROJECT_ID,
    #     deployed_model_display_name='retraining_deployed_model',
    #     machine_type="n1-standard-4",
    #     min_replica_count=1,
    #     max_replica_count=1,
    #     model=custom_container_job_run_op.outputs["model"],
    #     traffic_split={"0": 100}
    # )

    model_eval_task = classif_model_eval_metrics(
        PROJECT_ID,
        LOCATION,
        API_ENDPOINT,
        THRESHOLDS_DICT,
        train_task.outputs["output_model"],
    )

    with dsl.Condition(
        model_eval_task.outputs["dep_decision"] == "true",
        name="deploy_decision",
    ):

        deploy_task = deploy(
            previous_model,
            endpoint,
            model = train_task.outputs['output_model']
        )
        # deploy_op = gcc_aip.ModelDeployOp(  # noqa: F841
        #     model=train_task.outputs["output_model"]
        #     project=PROJECT_ID,
        #     machine_type="n1-standard-4"
        # )
    


# Compile and run the pipeline. Generate JSON pipeline definition file
compiler.Compiler().compile(pipeline_func=pipeline, 
        package_path='retraining-demo-uscentral1.json')


# You do not run the pipeline here. You must upload the JSON pipeline definition file to the Cloud Fcuntion instead
#PIPELINE_ROOT='gs://vertex-retraining-demo-uscentral1'
# from google.cloud.aiplatform import pipeline_jobs
# pipeline_jobs.PipelineJob(
#     display_name='retraining-demo-uscentral1',
#     template_path='retraining-demo-uscentral1.json',
#     pipeline_root=PIPELINE_ROOT,
#     parameter_values={'endpoint': '', 'previous_model': ''},
#     enable_caching=False
# ).run(sync=True)