
"""
The BST model uses the Transformer layer in its architecture to capture the sequential signals underlying
users’ behavior sequences for recommendation.
"""

import os
import math
import numpy as np
import pandas as pd
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
from tensorflow.keras.layers import StringLookup

"""## Prepare the data"""

ratings=pd.read_csv("train.csv")

"""Here, we do some simple data processing to fix the data types of the columns."""

ratings["itemId"] = ratings["itemId"].apply(lambda x: f"item_{x}")
ratings["userId"] = ratings["userId"].apply(lambda x: f"user_{x}")
ratings["rating"] = ratings["rating"].apply(lambda x: float(x))

"""### Transform the item ratings data into sequences

First, let's sort the the ratings data using the `unix_timestamp`, and then group the
`item_id` values and the `rating` values by `user_id`.

The output DataFrame will have a record for each `user_id`, with two ordered lists
(sorted by rating datetime): the items they have rated, and their ratings of these items.
"""

ratings_group = ratings.sort_values(by=["date"]).groupby("userId")

ratings_data = pd.DataFrame(
    data={
        "userId": list(ratings_group.groups.keys()),
        "itemId": list(ratings_group.itemId.apply(list)),
        "ratings": list(ratings_group.rating.apply(list)),
        "timestamps": list(ratings_group.date.apply(list)),
    }
)

"""Now, let's split the `item_ids` list into a set of sequences of a fixed length.
We do the same for the `ratings`. Set the `sequence_length` variable to change the length
of the input sequence to the model. You can also change the `step_size` to control the
number of sequences to generate for each user.
"""

sequence_length = 4
step_size = 2


def create_sequences(values, window_size, step_size):
    sequences = []
    start_index = 0
    while True:
        end_index = start_index + window_size
        seq = values[start_index:end_index]
        if len(seq) < window_size:
            seq = values[-window_size:]
            if len(seq) == window_size:
                sequences.append(seq)
            break
        sequences.append(seq)
        start_index += step_size
    return sequences


ratings_data.itemId = ratings_data.itemId.apply(
    lambda ids: create_sequences(ids, sequence_length, step_size)
)

ratings_data.ratings = ratings_data.ratings.apply(
    lambda ids: create_sequences(ids, sequence_length, step_size)
)

del ratings_data["timestamps"]

ratings_data=ratings_data[ratings_data["ratings"].str.len() != 0] #remove empty ratings

"""After that, we process the output to have each sequence in a separate records in
the DataFrame. In addition, we join the user features with the ratings data.
"""

ratings_data_items = ratings_data[["userId", "itemId"]].explode("itemId", ignore_index=True)
ratings_data_rating = ratings_data[["ratings"]].explode("ratings", ignore_index=True)
ratings_data_transformed = pd.concat([ratings_data_items, ratings_data_rating], axis=1)

ratings_data_transformed.itemId = ratings_data_transformed.itemId.apply(lambda x: ",".join(x))
ratings_data_transformed.ratings = ratings_data_transformed.ratings.apply(lambda x: ",".join([str(v) for v in x]))

# del ratings_data_transformed["zip_code"]

ratings_data_transformed.rename(
    columns={"itemId": "sequence_item_ids", "ratings": "sequence_ratings"},
    inplace=True,
)

"""Finally, we split the data into training and testing splits, with 85% and 15% of
the instances, respectively, and store them to CSV files.
"""

random_selection = np.random.rand(len(ratings_data_transformed.index)) <= 0.85
train_data = ratings_data_transformed[random_selection]
test_data = ratings_data_transformed[~random_selection]

train_data.to_csv("train_data.csv", index=False, sep="|", header=False)
test_data.to_csv("test_data.csv", index=False, sep="|", header=False)

"""## Define metadata"""

CSV_HEADER = list(ratings_data_transformed.columns)

CATEGORICAL_FEATURES_WITH_VOCABULARY = {
    "user_id": list(ratings.userId.unique()),
    "itemId": list(ratings.itemId.unique()),
}

"""## Create `tf.data.Dataset` for training and evaluation"""

def get_dataset_from_csv(csv_file_path, shuffle=False, batch_size=128):
    def process(features):
        item_ids_string = features["sequence_item_ids"]
        sequence_item_ids = tf.strings.split(item_ids_string, ",").to_tensor()

        # The last item id in the sequence is the target item.
        features["target_item_id"] = sequence_item_ids[:, -1]
        features["sequence_item_ids"] = sequence_item_ids[:, :-1]

        ratings_string = features["sequence_ratings"]
        sequence_ratings = tf.strings.to_number(
            tf.strings.split(ratings_string, ","), tf.dtypes.float32
        ).to_tensor()

        # The last rating in the sequence is the target for the model to predict.
        target = sequence_ratings[:, -1]
        features["sequence_ratings"] = sequence_ratings[:, :-1]

        return features, target

    dataset = tf.data.experimental.make_csv_dataset(
        csv_file_path,
        batch_size=batch_size,
        column_names=CSV_HEADER,
        num_epochs=1,
        header=False,
        field_delim="|",
        shuffle=shuffle,
    ).map(process)

    return dataset

"""## Create model inputs"""

def create_model_inputs():
    return {
        "userId": layers.Input(name="user_id", shape=(1,), dtype=tf.string),
        "sequence_item_ids": layers.Input(
            name="sequence_item_ids", shape=(sequence_length - 1,), dtype=tf.string
        ),
        "target_item_id": layers.Input(
            name="target_item_id", shape=(1,), dtype=tf.string
        ),
        "sequence_ratings": layers.Input(
            name="sequence_ratings", shape=(sequence_length - 1,), dtype=tf.float32
        ),
    }

"""## Encode input features"""

def encode_input_features(
    inputs,
    include_user_id=True,
    include_user_features=False,
    include_item_features=False,
):

    encoded_transformer_features = []
    encoded_other_features = []

    other_feature_names = []
    if include_user_id:
        other_feature_names.append("user_id")
    if include_user_features:
        other_feature_names.extend(USER_FEATURES)

    ## Encode user features
    for feature_name in other_feature_names:
        # Convert the string input values into integer indices.
        vocabulary = CATEGORICAL_FEATURES_WITH_VOCABULARY[feature_name]
        idx = StringLookup(vocabulary=vocabulary, mask_token=None, num_oov_indices=0)(
            inputs[feature_name]
        )
        # Compute embedding dimensions
        embedding_dims = int(math.sqrt(len(vocabulary)))
        # Create an embedding layer with the specified dimensions.
        embedding_encoder = layers.Embedding(
            input_dim=len(vocabulary),
            output_dim=embedding_dims,
            name=f"{feature_name}_embedding",
        )
        # Convert the index values to embedding representations.
        encoded_other_features.append(embedding_encoder(idx))

    ## Create a single embedding vector for the user features
    if len(encoded_other_features) > 1:
        encoded_other_features = layers.concatenate(encoded_other_features)
    elif len(encoded_other_features) == 1:
        encoded_other_features = encoded_other_features[0]
    else:
        encoded_other_features = None

    ## Create a item embedding encoder
    item_vocabulary = CATEGORICAL_FEATURES_WITH_VOCABULARY["itemId"]
    item_embedding_dims = int(math.sqrt(len(item_vocabulary)))
    # Create a lookup to convert string values to integer indices.
    item_index_lookup = StringLookup(
        vocabulary=item_vocabulary,
        mask_token=None,
        num_oov_indices=0,
        name="item_index_lookup",
    )
    # Create an embedding layer with the specified dimensions.
    item_embedding_encoder = layers.Embedding(
        input_dim=len(item_vocabulary),
        output_dim=item_embedding_dims,
        name=f"item_embedding",
    )
    # Create a processing layer for genres.
    item_embedding_processor = layers.Dense(
        units=item_embedding_dims,
        activation="relu",
        name="process_item_embedding_with_genres",
    )

    ## Define a function to encode a given item id.
    def encode_item(item_id):
        # Convert the string input values into integer indices.
        item_idx = item_index_lookup(item_id)
        item_embedding = item_embedding_encoder(item_idx)
        encoded_item = item_embedding
        if include_item_features:
            item_genres_vector = item_genres_lookup(item_idx)
            encoded_item = item_embedding_processor(
                layers.concatenate([item_embedding, item_genres_vector])
            )
        return encoded_item

    ## Encoding target_item_id
    target_item_id = inputs["target_item_id"]
    encoded_target_item = encode_item(target_item_id)

    ## Encoding sequence item_ids.
    sequence_items_ids = inputs["sequence_item_ids"]
    encoded_sequence_items = encode_item(sequence_items_ids)
    # Create positional embedding.
    position_embedding_encoder = layers.Embedding(
        input_dim=sequence_length,
        output_dim=item_embedding_dims,
        name="position_embedding",
    )
    positions = tf.range(start=0, limit=sequence_length - 1, delta=1)
    encodded_positions = position_embedding_encoder(positions)
    # Retrieve sequence ratings to incorporate them into the encoding of the item.
    sequence_ratings = tf.expand_dims(inputs["sequence_ratings"], -1)
    # Add the positional encoding to the item encodings and multiply them by rating.
    encoded_sequence_items_with_poistion_and_rating = layers.Multiply()(
        [(encoded_sequence_items + encodded_positions), sequence_ratings]
    )

    # Construct the transformer inputs.
    for encoded_item in tf.unstack(
        encoded_sequence_items_with_poistion_and_rating, axis=1
    ):
        encoded_transformer_features.append(tf.expand_dims(encoded_item, 1))
    encoded_transformer_features.append(encoded_target_item)

    encoded_transformer_features = layers.concatenate(
        encoded_transformer_features, axis=1
    )

    return encoded_transformer_features, encoded_other_features

"""## Create a BST model"""

include_user_id = False
include_user_features = False
include_item_features = False

hidden_units = [256, 128]
dropout_rate = 0.1
num_heads = 3


def create_model():
    inputs = create_model_inputs()
    transformer_features, other_features = encode_input_features(
        inputs, include_user_id, include_user_features, include_item_features
    )

    # Create a multi-headed attention layer.
    attention_output = layers.MultiHeadAttention(
        num_heads=num_heads, key_dim=transformer_features.shape[2], dropout=dropout_rate
    )(transformer_features, transformer_features)

    # Transformer block.
    attention_output = layers.Dropout(dropout_rate)(attention_output)
    x1 = layers.Add()([transformer_features, attention_output])
    x1 = layers.LayerNormalization()(x1)
    x2 = layers.LeakyReLU()(x1)
    x2 = layers.Dense(units=x2.shape[-1])(x2)
    x2 = layers.Dropout(dropout_rate)(x2)
    transformer_features = layers.Add()([x1, x2])
    transformer_features = layers.LayerNormalization()(transformer_features)
    features = layers.Flatten()(transformer_features)

    # Included the other features.
    if other_features is not None:
        features = layers.concatenate(
            [features, layers.Reshape([other_features.shape[-1]])(other_features)]
        )

    # Fully-connected layers.
    for num_units in hidden_units:
        features = layers.Dense(num_units)(features)
        features = layers.BatchNormalization()(features)
        features = layers.LeakyReLU()(features)
        features = layers.Dropout(dropout_rate)(features)

    outputs = layers.Dense(units=1)(features)
    model = keras.Model(inputs=inputs, outputs=outputs)
    return model


model = create_model()

"""## Run training and evaluation experiment"""

# Compile the model.
model.compile(
    optimizer=keras.optimizers.Adagrad(learning_rate=0.01),
    loss=keras.losses.MeanSquaredError(),
    metrics=[keras.metrics.MeanAbsoluteError()],
)

# Read the training data.
train_dataset = get_dataset_from_csv("train_data.csv", shuffle=True, batch_size=265)

# Fit the model with the training data.
model.fit(train_dataset, epochs=5)

# Read the test data.
test_dataset = get_dataset_from_csv("test_data.csv", batch_size=265)

# Evaluate the model on the test data.
_, rmse = model.evaluate(test_dataset, verbose=0)
print(f"Test MAE: {round(rmse, 3)}")

"""
OutPut:
Epoch 1/5
13772/13772 [==============================] - 2155s 156ms/step - loss: 0.8937 - mean_absolute_error: 0.7369
Epoch 2/5
13772/13772 [==============================] - 2170s 158ms/step - loss: 0.8073 - mean_absolute_error: 0.6984
Epoch 3/5
13772/13772 [==============================] - 2255s 164ms/step - loss: 0.7889 - mean_absolute_error: 0.6895
Epoch 4/5
13772/13772 [==============================] - 2141s 155ms/step - loss: 0.7781 - mean_absolute_error: 0.6844
Epoch 5/5
13772/13772 [==============================] - 2114s 153ms/step - loss: 0.7701 - mean_absolute_error: 0.6806
Test MAE: 0.673
"""

