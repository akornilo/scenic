"""Implementation of the OWL-ViT detection model."""

from typing import Any, Dict, List, Mapping, Optional

import flax.linen as nn
from flax.training import checkpoints
import jax.numpy as jnp
import ml_collections
from scenic.projects.owl_vit import layers
from scenic.projects.owl_vit import matching_base_models
from scenic.projects.owl_vit import utils
from scenic.projects.owl_vit.clip import model as clip_model
from scenic.projects.owl_vit.clip import tokenizer as clip_tokenizer

Params = layers.Params


class TextZeroShotDetectionModule(nn.Module):
  """Text-query-based OWL-ViT model.

  This module computes joint text and image embeddings which are then
  used for localized prediction of bounding boxes and classes.

  Attributes:
    body_configs: Configurations of the image-text module.
    normalize: Whether to normalize the output of the model and the
      label_embeddings before computing the class logits.
    box_bias: Type of box bias - one of 'location', 'size' or 'both'.
  """

  body_configs: ml_collections.ConfigDict
  normalize: bool = False
  box_bias: str = 'both'

  def tokenize(self, text: str, max_token_len: int = 16) -> List[int]:
    return clip_tokenizer.tokenize(text, max_token_len)

  @nn.nowrap
  def load_variables(self, checkpoint_path: str) -> Mapping[str, Any]:
    restored = checkpoints.restore_checkpoint(checkpoint_path, target=None)

    if 'params' in restored:
        return {'params': restored['params']}
    else:
        return {'params': restored['optimizer']['target']}

  def setup(self):
    self._embedder = layers.ClipImageTextEmbedder(
        self.body_configs, name='backbone')
    self._class_head = layers.ClassPredictor(
        out_dim=clip_model.CONFIGS[self.body_configs.variant]['embed_dim'],
        normalize=self.normalize, name='class_head')
    self._box_head = layers.PredictorMLP(
        mlp_dim=None, out_dim=4, num_layers=3,
        out_activation=None, name='obj_box_head')

  def box_predictor(self, image_features: jnp.ndarray,
                    feature_map: jnp.ndarray) -> Dict[str, jnp.ndarray]:
    """Predicts bounding boxes from image features.

    Args:
      image_features: Feature tokens extracted from the image, returned by the
        `embedder` function.
      feature_map: A spatial re-arrangement of image_features, also returned by
        the `embedder` function.

    Returns:
      List of predicted boxes (cxcywh normalized to 0, 1) nested within
        a dictionary.
    """
    # Bounding box detection head [b, num_patches, 4].
    pred_boxes = self._box_head(image_features)
    # We compute the location of each token on the grid and use it to compute
    # a bias for the bbox prediction, i.e., each token is biased towards
    # predicting its location on the grid as the center.
    pred_boxes += utils.compute_box_bias(feature_map, kind=self.box_bias)
    pred_boxes = nn.sigmoid(pred_boxes)
    return {'pred_boxes': pred_boxes}

  def class_predictor(
      self,
      image_features: jnp.ndarray,
      query_embeddings: Optional[jnp.ndarray] = None,
      query_mask: Optional[jnp.ndarray] = None) -> Dict[str, jnp.ndarray]:
    """Applies the class head to the image features.

    Args:
      image_features: Feature tokens extracted by the image embedder.
      query_embeddings: Optional list of text (or image) embeddings. If no
        embeddings are provided, no logits will be computed and only the class
        embeddings for the image will be returned.
      query_mask: Must be provided with query_embeddings. A mask indicating
        which query embeddings are valid.

    Returns:
      A dictionary containing the class_embeddings and the pred_logits if
        query_embeddings and query_mask are provided.
    """
    return self._class_head(image_features, query_embeddings, query_mask)

  def image_embedder(self, images: jnp.ndarray, train: bool) -> jnp.ndarray:
    """Embeds images into feature maps.

    Args:
      images: images of shape (batch, input_size, input_size, 3), scaled to the
        input range defined in the config. Padding should be at the bottom right
        of the image.
      train: Whether or not we are in training mode.

    Returns:
      A 2D map of image features.
    """
    image_features, _ = self._embedder(images=images, train=train)
    return utils.seq2img(images, image_features)

  def text_embedder(self, text_queries: jnp.ndarray,
                    train: bool) -> jnp.ndarray:
    """Embeds text into features.

    Args:
      text_queries: jnp.int32 tokenized text queries of shape [..., num_tokens].
      train: Whether or not we are in training mode.

    Returns:
      An array of the same shape as text_queries, except for the last dimension,
      which is num_dimensions instead of num_tokens.
    """
    _, text_features = self._embedder(texts=text_queries, train=train)
    return text_features

  def __call__(self,
               inputs: jnp.ndarray,
               text_queries: jnp.ndarray,
               train: bool,
               *,
               debug: bool = False) -> Mapping[str, Any]:
    """Applies TextZeroShotDetectionModule on the input.

    Args:
      inputs: Images [batch_size, height, width, 3].
      text_queries: Queries to score boxes on. Queries starting with 0 stand for
        padding [batch_size=b, num_queries=q, max_query_length=l].
      train: Whether it is training.
      debug: Unused.

    Returns:
      Outputs dict with items:
        pred_logits: Class logits [b, num_patches, num_queries].
        pred_boxes: Predicted bounding boxes [b, num_patches, 4].
        feature_map: Image embeddings 2d feature map [b, sp, sp, img_emb_dim].
    """
    del debug
    # Embed images:
    feature_map = self.image_embedder(inputs, train)
    b, h, w, d = feature_map.shape
    image_features = jnp.reshape(feature_map, (b, h * w, d))

    # Embed queries:
    query_embeddings = self.text_embedder(text_queries, train)
    # If first token is 0, then this is a padding query [b, q].
    query_mask = (text_queries[..., 0] > 0).astype(jnp.float32)

    outputs = {
        'feature_map': feature_map,
        'query_embeddings': query_embeddings,
    }

    # Classification [b, num_patches, num_queries]:
    outputs.update(
        self.class_predictor(image_features, query_embeddings, query_mask))

    # Predict boxes:
    outputs.update(self.box_predictor(image_features, feature_map))

    return outputs

  def load(
      self, params: Params,
      init_config: ml_collections.ConfigDict) -> Params:
    """Loads backbone parameters for this model from a backbone checkpoint."""
    if init_config.get('codebase') == 'clip':
      # Initialize backbone parameters from an external codebase.
      params['backbone'] = self._embedder.load_backbone(
          params['backbone'], init_config.get('checkpoint_path'))
    else:
      # Initialize all parameters from a Scenic checkpoint.
      restored_train_state = checkpoints.restore_checkpoint(
          init_config.checkpoint_path, target=None)
      if 'optimizer' in restored_train_state:
        # Pre-Optax checkpoint:
        params = restored_train_state['optimizer']['target']
      else:
        params = restored_train_state['params']

      # explicitly removing unused parameters after loading
      params['class_head'].pop('padding', None)
      params['class_head'].pop('padding_bias', None)
    return params


class TextZeroShotDetectionModel(matching_base_models.ObjectDetectionModel):
  """OWL-ViT model for detection."""

  def build_flax_model(self) -> nn.Module:
    return TextZeroShotDetectionModule(
        body_configs=self.config.model.body,
        normalize=self.config.model.normalize,
        box_bias=self.config.model.box_bias)
