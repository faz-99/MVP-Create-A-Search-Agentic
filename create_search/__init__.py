"""Agentic Create Search.

Turns a natural-language research goal into one executable Intelligize search payload,
plus a search strategy and analysis instructions. Two agent loops share everything in
`create_search.core`; only the loop that drives the model differs:

  agents/openai_agent.py   OpenAI Agents SDK (the long-term runtime, as in agentv4)
  agents/claude_agent.py   our own loop on the Anthropic SDK (Bedrock)

Entry point for callers: `create_search.run.create_search`.
"""
