# Ledger Linker

A minimal landing page built with Jekyll for GitHub Pages, with a primary link to [DataflowPH](https://jhoibclemente.github.io/dataflowph-site/index.html#approach).

## Local development

Use Ruby 3.1 and Bundler 2.6.9:

```sh
bundle install
bundle exec jekyll serve --baseurl /ledger_linker
```

In the prepared cloud environment, activate the retained Ruby tools and explicitly select this repository's bundle:

```sh
source /workspace/.onboarding/ledger_linker/activate.sh
export BUNDLE_GEMFILE=/workspace/ledger_linker/Gemfile
bundle check
bundle exec jekyll serve --baseurl /ledger_linker
```

## Build and deploy

```sh
JEKYLL_ENV=production bundle exec jekyll build --baseurl /ledger_linker
```

In repository settings, set **Pages → Build and deployment → Source** to **GitHub Actions**. The included workflow builds pushes to `main` and deploys the generated site. Deployment is successful only when its Actions run completes and the published URL responds with the expected page.
