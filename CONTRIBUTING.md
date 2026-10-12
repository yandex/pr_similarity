# Notice to external contributors

## General info

Hello! In order for us (YANDEX LLC) to accept patches and other contributions from you, you will have to adopt our Contributor License Agreement (the “**CLA**”). The current version of the CLA you may find here:

1. https://yandex.ru/legal/cla/en/ (in English) and
2. https://yandex.ru/legal/cla/ru/ (in Russian).

By adopting the CLA, you state the following:

* You obviously wish and are willingly licensing your contributions to us for our open source projects under the terms of the CLA,
* You have read the terms and conditions of the CLA and agree with them in full,
* You are legally able to provide and license your contributions as stated,
* We may use your contributions for our open source projects and for any other our project too,
* We rely on your assurances concerning the rights of third parties in relation to your contributions.

If you agree with these principles, please read and adopt our CLA. By providing us your contributions, you hereby declare that you have read and adopted our CLA, and we may freely merge your contributions with our corresponding open source project and use it in further in accordance with terms and conditions of the CLA.

## Provide contributions

If you have adopted terms and conditions of the CLA, you are able to provide your contributions. When you submit your pull request, please add the following information into it:

```
I hereby agree to the terms of the CLA available at: [link].
```

Replace the bracketed text as follows:

* \[link\] is the link at the current version of the CLA (you may add here a link <https://yandex.ru/legal/cla/?lang=en> (in English) or a link <https://yandex.ru/legal/cla/?lang=ru> (in Russian).

It is enough to provide us with such notification once.

## Other questions

If you have any questions, please write us at [opensource-support@yandex-team.ru](mailto:opensource-support@yandex-team.ru) .

## Development

PR Similarity has three independent modules, each with its own virtual environment.
Read the root README and the README of the module you change. Run commands from
that module; collector and benchmark require Python 3.12, viewer supports 3.12 or newer.

```sh
make setup PYTHON=python3.12
make test
```

Collector and benchmark also require `make smoke`. Agent execution requires Linux
x64/glibc and working bubblewrap user namespaces. Viewer browser tests require
`make browser-setup` and the documented Playwright system libraries.

Keep changes scoped, update affected file/API producers and consumers together,
and synchronize documentation when behavior changes. Tests use offline fixtures;
model calls, live collection and publication are separate operator actions.
Do not include credentials, private configs, caches or run artifacts in a pull request.
