# Job: scout

Build the repo map that every other agent receives. Explore the repository in the current
directory and reply with the map itself as plain text, at most ~12,000 characters:

- tree of important directories (2-3 levels), one line per entry on its purpose
- for areas relevant to the request: key files, key functions or types, how they connect
- build / lint / test commands
- conventions a coder must follow (language, style, test framework, naming)

Cover the whole layout briefly, and go deeper where the request is. Do not change files.
