from agents import Agent, RunHooks
from agents.items import TResponseInputItem
from agents.run_context import RunContextWrapper

class MultiRunHooks(RunHooks):
    def __init__(self, *hooks: RunHooks):
        self._hooks = hooks

    async def on_llm_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        for hook in self._hooks:
            call = getattr(hook, "on_llm_start", None)
            if callable(call):
                await hook.on_llm_start(context, agent, system_prompt, input_items)

    async def on_llm_end(self, context, agent, response) -> None:
        for hook in self._hooks:
            call = getattr(hook, "on_llm_end", None)
            if callable(call):
                await hook.on_llm_end(context, agent, response)

    async def on_agent_start(self, context, agent) -> None:
        for hook in self._hooks:
            call = getattr(hook, "on_agent_start", None)
            if callable(call):
                await hook.on_agent_start(context, agent)

    async def on_agent_end(self, context, agent, output) -> None:
        for hook in self._hooks:
            call = getattr(hook, "on_agent_end", None)
            if callable(call):
                await hook.on_agent_end(context, agent, output)
