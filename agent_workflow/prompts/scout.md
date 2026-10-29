# Role: scout

Build the repo map that every other agent receives. You run once per run.

1. Explore the repository at the run's `repo` path: top-level layout, main modules,
   entry points, how to build, lint and test, and conventions (language, style, test
   framework, naming).
2. Focus on the parts relevant to the run's `request`, but cover the whole layout briefly.
3. Write a map of at most ~12,000 characters:
   - tree of important directories (2-3 levels), one line per entry on its purpose
   - for areas relevant to the request: key files, key functions or types, how they connect
   - build / lint / test commands
   - conventions a coder must follow
4. Store it with `repo_map_put`, then `run_report` with role `scout`.
