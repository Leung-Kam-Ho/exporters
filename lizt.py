from exporters.coreml.features import FeaturesManager
distilbert_features = list(FeaturesManager.get_supported_features_for_model_type("mobilebert").keys())
print(distilbert_features)
['default', 'masked-lm', 'multiple-choice', 'question-answering', 'sequence-classification', 'token-classification']