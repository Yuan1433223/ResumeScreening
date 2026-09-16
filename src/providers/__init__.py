# -*- coding: utf-8 -*-
"""抽取器 providers 包"""
from .mock_extractor import MockExtractor
from .model_extractor import ModelExtractor

__all__ = ["MockExtractor", "ModelExtractor"]
