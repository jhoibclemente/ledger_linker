# Ledger Linker

A bank statement workspace with regular-user and administrator accounts, PDF
extraction, reviewed CSV aggregation, private submission history and admin settings.

The full application runs on Render with a persistent disk. GitHub Pages hosts
the landing page and static login UI only. See [DEPLOYMENT.md](DEPLOYMENT.md) for
deployment, account setup, supported layouts and Python development instructions.

[Deploy application on Render](https://render.com/deploy?repo=https://github.com/jhoibclemente/ledger_linker)

## Local development

Use Ruby 3.3 (as specified in `.ruby-version`) and Bundler 2.6.9:

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
