# Copyright 2018, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import functools

import numpy as np
import tensorflow as tf

from tensorflow_federated.python import core as tff
from tensorflow_federated.python.common_libs import anonymous_tuple
from tensorflow_federated.python.common_libs import test
from tensorflow_federated.python.learning import model_examples
from tensorflow_federated.python.learning import model_utils
from tensorflow_federated.python.learning.framework import optimizer_utils


class DummyClientDeltaFn(optimizer_utils.ClientDeltaFn):

  def __init__(self, model_fn):
    self._model = model_fn()
    self._optimizer = tf.keras.optimizers.SGD(learning_rate=0.1)

  @property
  def variables(self):
    return []

  @tf.function
  def __call__(self, dataset, initial_weights):
    # Iterate over the dataset to get new metric values.
    def reduce_fn(dummy, batch):
      with tf.GradientTape() as tape:
        output = self._model.forward_pass(batch)
      gradients = tape.gradient(output.loss, self._model.trainable_variables)
      self._optimizer.apply_gradients(
          zip(gradients, self._model.trainable_variables))
      return dummy

    dataset.reduce(tf.constant(0.0), reduce_fn)

    # Create some fake weight deltas to send back.
    trainable_weights_delta = tf.nest.map_structure(lambda x: -tf.ones_like(x),
                                                    initial_weights.trainable)
    client_weight = tf.constant(1.0)
    return optimizer_utils.ClientOutput(
        trainable_weights_delta,
        weights_delta_weight=client_weight,
        model_output=self._model.report_local_outputs(),
        optimizer_output=collections.OrderedDict([('client_weight',
                                                   client_weight)]))


def _state_incrementing_mean_next(server_state, client_value, weight=None):
  add_one = tff.tf_computation(lambda x: x + 1, tf.int32)
  new_state = tff.federated_map(add_one, server_state)
  return (new_state, tff.federated_mean(client_value, weight=weight))


state_incrementing_mean = tff.utils.StatefulAggregateFn(
    lambda: tf.constant(0), _state_incrementing_mean_next)


def _state_incrementing_broadcast_next(server_state, server_value):
  add_one = tff.tf_computation(lambda x: x + 1, tf.int32)
  new_state = tff.federated_map(add_one, server_state)
  return (new_state, tff.federated_broadcast(server_value))


state_incrementing_broadcaster = tff.utils.StatefulBroadcastFn(
    lambda: tf.constant(0), _state_incrementing_broadcast_next)


class UtilsTest(test.TestCase):

  def test_state_with_new_model_weights(self):
    trainable = [np.array([1.0, 2.0]), np.array([[1.0]])]
    non_trainable = [np.array(1)]
    state = anonymous_tuple.from_container(
        optimizer_utils.ServerState(
            model=model_utils.ModelWeights(
                trainable=trainable, non_trainable=non_trainable),
            optimizer_state=[],
            delta_aggregate_state=tf.constant(0),
            model_broadcast_state=tf.constant(0)),
        recursive=True)

    new_state = optimizer_utils.state_with_new_model_weights(
        state,
        trainable_weights=[np.array([3.0, 3.0]),
                           np.array([[3.0]])],
        non_trainable_weights=[np.array(3)])
    self.assertAllClose(
        new_state.model.trainable,
        [np.array([3.0, 3.0]), np.array([[3.0]])])
    self.assertAllClose(new_state.model.non_trainable, [3])

    with self.assertRaisesRegex(TypeError, 'tensor type'):
      optimizer_utils.state_with_new_model_weights(
          state,
          trainable_weights=[np.array([3.0, 3.0]),
                             np.array([[3]])],
          non_trainable_weights=[np.array(3.0)])

    with self.assertRaisesRegex(TypeError, 'tensor type'):
      optimizer_utils.state_with_new_model_weights(
          state,
          trainable_weights=[np.array([3.0, 3.0]),
                             np.array([3.0])],
          non_trainable_weights=[np.array(3)])

    with self.assertRaisesRegex(TypeError, 'different lengths'):
      optimizer_utils.state_with_new_model_weights(
          state,
          trainable_weights=[np.array([3.0, 3.0])],
          non_trainable_weights=[np.array(3)])

    with self.assertRaisesRegex(TypeError, 'cannot be handled'):
      optimizer_utils.state_with_new_model_weights(
          state,
          trainable_weights={'a': np.array([3.0, 3.0])},
          non_trainable_weights=[np.array(3)])


class ModelDeltaOptimizerTest(test.TestCase):

  def test_contruction(self):
    iterative_process = optimizer_utils.build_model_delta_optimizer_process(
        model_fn=model_examples.LinearRegression,
        model_to_client_delta_fn=DummyClientDeltaFn,
        server_optimizer_fn=tf.keras.optimizers.SGD)

    server_state_type = tff.FederatedType(
        optimizer_utils.ServerState(
            model=model_utils.ModelWeights(
                trainable=[
                    tff.TensorType(tf.float32, [2, 1]),
                    tff.TensorType(tf.float32)
                ],
                non_trainable=[tff.TensorType(tf.float32)]),
            optimizer_state=[tf.int64],
            delta_aggregate_state=(),
            model_broadcast_state=()), tff.SERVER)

    self.assertEqual(iterative_process.initialize.type_signature,
                     tff.FunctionType(parameter=None, result=server_state_type))

    dataset_type = tff.FederatedType(
        tff.SequenceType(
            collections.OrderedDict(
                x=tff.TensorType(tf.float32, [None, 2]),
                y=tff.TensorType(tf.float32, [None, 1]))), tff.CLIENTS)

    metrics_type = tff.FederatedType(
        collections.OrderedDict(
            loss=tff.TensorType(tf.float32),
            num_examples=tff.TensorType(tf.int32)), tff.SERVER)

    self.assertEqual(
        iterative_process.next.type_signature,
        tff.FunctionType(
            parameter=(server_state_type, dataset_type),
            result=(server_state_type, metrics_type)))

  def test_contruction_with_adam_optimizer(self):
    iterative_process = optimizer_utils.build_model_delta_optimizer_process(
        model_fn=model_examples.LinearRegression,
        model_to_client_delta_fn=DummyClientDeltaFn,
        server_optimizer_fn=tf.keras.optimizers.Adam)
    # Assert that the optimizer_state includes the 5 variables (scalar for
    # # of iterations, plus two copies of the kernel and bias in the model).
    initialize_type = iterative_process.initialize.type_signature
    self.assertLen(initialize_type.result.member.optimizer_state, 5)

    next_type = iterative_process.next.type_signature
    self.assertLen(next_type.parameter[0].member.optimizer_state, 5)
    self.assertLen(next_type.result[0].member.optimizer_state, 5)

  def test_contruction_with_aggregator_state(self):
    iterative_process = optimizer_utils.build_model_delta_optimizer_process(
        model_fn=model_examples.LinearRegression,
        model_to_client_delta_fn=DummyClientDeltaFn,
        server_optimizer_fn=tf.keras.optimizers.SGD,
        stateful_delta_aggregate_fn=state_incrementing_mean)

    expected_aggregate_state_type = tff.TensorType(tf.int32)

    initialize_type = iterative_process.initialize.type_signature
    self.assertEqual(initialize_type.result.member.delta_aggregate_state,
                     expected_aggregate_state_type)

    next_type = iterative_process.next.type_signature
    self.assertEqual(next_type.parameter[0].member.delta_aggregate_state,
                     expected_aggregate_state_type)
    self.assertEqual(next_type.result[0].member.delta_aggregate_state,
                     expected_aggregate_state_type)

  def test_contruction_with_broadcast_state(self):
    iterative_process = optimizer_utils.build_model_delta_optimizer_process(
        model_fn=model_examples.LinearRegression,
        model_to_client_delta_fn=DummyClientDeltaFn,
        server_optimizer_fn=tf.keras.optimizers.SGD,
        stateful_model_broadcast_fn=state_incrementing_broadcaster)

    expected_broadcast_state_type = tff.TensorType(tf.int32)

    initialize_type = iterative_process.initialize.type_signature
    self.assertEqual(initialize_type.result.member.model_broadcast_state,
                     expected_broadcast_state_type)

    next_type = iterative_process.next.type_signature
    self.assertEqual(next_type.parameter[0].member.model_broadcast_state,
                     expected_broadcast_state_type)
    self.assertEqual(next_type.result[0].member.model_broadcast_state,
                     expected_broadcast_state_type)

  def test_orchestration_execute_sgd(self):
    learning_rate = 1.0
    iterative_process = optimizer_utils.build_model_delta_optimizer_process(
        model_fn=model_examples.LinearRegression,
        model_to_client_delta_fn=DummyClientDeltaFn,
        server_optimizer_fn=functools.partial(
            tf.keras.optimizers.SGD, learning_rate=learning_rate),
        # A federated_mean that maintains an int32 state equal to the
        # number of times the federated_mean has been executed,
        # allowing us to test that a stateful aggregator's state
        # is properly updated.
        stateful_delta_aggregate_fn=state_incrementing_mean,
        # Similarly, a broadcast with state that increments:
        stateful_model_broadcast_fn=state_incrementing_broadcaster)

    ds = tf.data.Dataset.from_tensor_slices(
        collections.OrderedDict([
            ('x', [[1.0, 2.0], [3.0, 4.0]]),
            ('y', [[5.0], [6.0]]),
        ])).batch(2)
    federated_ds = [ds] * 3

    state = iterative_process.initialize()
    # SGD keeps track of a single scalar for the number of iterations.
    self.assertAllEqual(state.optimizer_state, [0])
    self.assertAllClose(list(state.model.trainable), [np.zeros((2, 1)), 0.0])
    self.assertAllClose(list(state.model.non_trainable), [0.0])
    self.assertEqual(state.delta_aggregate_state, 0)
    self.assertEqual(state.model_broadcast_state, 0)

    state, outputs = iterative_process.next(state, federated_ds)
    self.assertAllClose(
        list(state.model.trainable), [-np.ones((2, 1)), -1.0 * learning_rate])
    self.assertAllClose(list(state.model.non_trainable), [0.0])
    self.assertAllEqual(state.optimizer_state, [1])
    self.assertEqual(state.delta_aggregate_state, 1)
    self.assertEqual(state.model_broadcast_state, 1)

    # Since all predictions are 0, loss is:
    #    (0.5 * (0-5)^2 + (0-6)^2) / 2 = 15.25
    self.assertAlmostEqual(outputs.loss, 15.25, places=4)
    # 3 clients * 2 examples per client = 6 examples.
    self.assertAlmostEqual(outputs.num_examples, 6.0, places=8)


if __name__ == '__main__':
  test.main()
