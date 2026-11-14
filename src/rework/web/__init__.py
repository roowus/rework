"""Outbound web access for roles: `web_search` (SearXNG) and `fetch_url`.

Both are ordinary role tools, registered compositionally the same way the
library tools are (see web/tools.py), and both stay behind the guard in
web/guard.py: a role may read the public web, never the deployment's own
network.
"""
