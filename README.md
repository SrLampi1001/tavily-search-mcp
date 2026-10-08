# Tavily search MCP
This is a web-search MCP created mainly for sub-agents (invoked agents) usage and implementation.
It uses the Free tier API Key (1000 credits monthly).

The objective is to offer an MCP that allows any sub agent and agent to make web searchs to retrieve latest and update data on the web.
This project is standalone.

## Objectives
- Include prompts to allow the agents and subagents know the current API usage and limitations, and to be conservative on the amount of request.
- Caching the API responses to prevent consumtion over repeated queries
- A mechanism that allows the user to repeat a cached query if they explicitly asks for, and a mechanism (maybe out of scope for now) that allows to identify if the source of the query has been updated, and therefore automatically updating the cache with a new query to the source.
- Include skills to allow the best practices into research summary and extraction in the MCP
- Use VS Code forwarding to expose the MCP (Ngirok is a valid fallback but won't be configured in this repo right now) with no auth to trusted hosts.
- Allow the MCP client side to configure it's own API key for Tavily.
- Allow the search and scrapping to get web results.
