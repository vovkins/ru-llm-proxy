# Уведомления о сторонних компонентах

## Tiny2

Источник: https://huggingface.co/Katerina-Bir/rubert-tiny2-secret-detection

Ревизия: `4137142e79d52b0b69370e0e3dceb2d6ad82591d`.
Базовая модель: `cointegrated/rubert-tiny2`. Вариант весов:
`tiny2-mix-a-s1-ft-spacy-free-20261013-a-e2-b2`.

Лицензия модели и обучающих данных: MIT; сведения об источниках и подтверждение
автора получены владельцем проекта и зафиксированы в задаче #110. Контрольные
суммы поставки находятся в `presidio/model_manifest.json`. Веса скачиваются
при сборке и не включаются в Git.

Triton и ONNX Runtime являются самостоятельными сторонними компонентами.
MIT модели не распространяется на образ NVIDIA и его зависимости; применяются
их собственные лицензии и [условия NVIDIA](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/tritonserver).

## Детерминированные правила

Детерминированные форматы ключей и токенов в
`presidio/recognizers/credential_rules.py` частично адаптированы из конфигурации
Gitleaks `config/gitleaks.toml`. Источник закреплён в
`presidio/evaluation/deterministic_rule_source_manifest.json`; правила перенесены
выборочно и переработаны под типы сущностей, границы значений и фильтрацию
заполнителей ru-llm-proxy.

## Gitleaks

Source: https://github.com/gitleaks/gitleaks

License: MIT

MIT License

Copyright (c) 2019 Zachary Rice

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
