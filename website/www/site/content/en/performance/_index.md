---
title: "Beam IO Performance"
---

<!--
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Beam IO Performance

Various Beam pipelines measure characteristics of reading from and writing to
various IOs.

# Available Metrics

Various metrics were gathered using the Beam SDK
[Metrics API](/documentation/programming-guide/#metrics)
from a pipeline Job running on [Dataflow](/documentation/runners/dataflow/).

See the [glossary](/performance/glossary) for a list of the metrics and their
definition.

# Measured Beam Java IOs

See the following pages for performance measures recorded when reading from and
writing to various Beam IOs.

- [BigQuery](/performance/bigquery)
- [BigTable](/performance/bigtable)
- [TextIO](/performance/textio)

# Measured Beam Python ML Pipelines

See the following pages for performance measures recorded when running various Beam ML pipelines.

## Streaming

- [PyTorch Sentiment Analysis Streaming DistilBERT base](/performance/pytorchbertsentimentstreaming)

## Batch

- [PyTorch Sentiment Analysis Batch DistilBERT base](/performance/pytorchbertsentimentbatch)
- [PyTorch Language Modeling BERT base](/performance/pytorchbertbase)
- [PyTorch Language Modeling BERT large](/performance/pytorchbertlarge)
- [PyTorch Vision Classification Resnet 101](/performance/pytorchresnet101)
- [PyTorch Vision Classification Resnet 152](/performance/pytorchresnet152)
- [PyTorch Vision Classification Resnet 152 Tesla T4 GPU](/performance/pytorchresnet152tesla)
- [TensorFlow MNIST Image Classification](/performance/tensorflowmnist)
