"""v0.12.17 render-mode context construction + tool_choice enforcement.

Covers:
  - the per-step rebuild shape (task first, one page state last, at most
    one native turn, journal for older steps)
  - post-call nudges surviving exactly one rebuild
  - render_keep_native_turns=0 (pure re-render + [LAST_TURN_RESULTS])
  - last-step tool_choice={"name": "done"} enforcement
  - "required" tool_choice after an empty model output
  - provider tool_choice mappings
"""

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import Agent  # noqa: E402
from browser_use_rs.llm.base import (  # noqa: E402
    AssistantMessage,
    BaseChatModel,
    ChatInvokeCompletion,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from browser_use_rs.tools import tool  # noqa: E402


@tool
async def poke(session) -> str:
    """Poke the page (test tool)."""
    return "poke ok"


def _msg_text(msg) -> str:
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            getattr(p, "text", "") for p in content if hasattr(p, "text")
        )
    return ""


def _texts(messages) -> list[str]:
    return [_msg_text(m) for m in messages]


class ScriptedLLM(BaseChatModel):
    """Returns queued completions; records each call's message list."""

    name = "scripted"
    model = "scripted"

    def __init__(self, completions):
        self.completions = list(completions)
        self.calls: list[dict] = []

    async def ainvoke(self, messages, tools, *, system=None):
        self.calls.append({"messages": list(messages), "tool_choice": None})
        return self.completions.pop(0)


class ScriptedToolChoiceLLM(ScriptedLLM):
    """Same, but accepts (and records) the tool_choice kwarg."""

    async def ainvoke(self, messages, tools, *, system=None, tool_choice=None):
        self.calls.append(
            {"messages": list(messages), "tool_choice": tool_choice}
        )
        return self.completions.pop(0)


def _make_agent(llm, **kwargs):
    kwargs.setdefault("tools", [poke])
    kwargs.setdefault("browser_session", object())
    kwargs.setdefault("auto_initial_navigation", False)
    return Agent("count the widgets on the page", llm, **kwargs)


def _poke_call(i):
    return ChatInvokeCompletion(
        text=f"<memory>step {i} noted</memory>",
        tool_calls=[ToolCall(id=f"call_{i}", name="poke", args={})],
    )


class RenderModeShapeTests(unittest.TestCase):
    def test_rebuild_shape_across_three_steps(self):
        llm = ScriptedLLM(
            [
                _poke_call(1),
                _poke_call(2),
                ChatInvokeCompletion(text="the result is 42"),
            ]
        )
        agent = _make_agent(llm)
        self.assertEqual("render", agent.context_mode)
        asyncio.run(agent.run())

        self.assertEqual(3, len(llm.calls))

        # Call 1: [task, page state] — nothing else exists yet.
        texts = _texts(llm.calls[0]["messages"])
        self.assertEqual(2, len(texts))
        self.assertIn("<user_request>", texts[0])
        self.assertTrue(texts[1].startswith("[PAGE_STATE]"))
        self.assertIn("<step_info>", texts[1])

        # Call 2: [task, native turn (assistant+tool result), state].
        # History has one item, carried natively → no journal yet.
        msgs = llm.calls[1]["messages"]
        self.assertIn("<user_request>", _msg_text(msgs[0]))
        self.assertFalse(
            any(t.startswith("[AGENT_HISTORY]") for t in _texts(msgs))
        )
        self.assertEqual(
            1, sum(1 for m in msgs if isinstance(m, AssistantMessage))
        )
        self.assertEqual(
            1, sum(1 for m in msgs if isinstance(m, ToolResultMessage))
        )
        self.assertTrue(_msg_text(msgs[-1]).startswith("[PAGE_STATE]"))

        # Call 3: step 1 aged out of native and into the journal;
        # exactly one native turn (step 2's) and one page state remain.
        msgs = llm.calls[2]["messages"]
        journal = [
            t for t in _texts(msgs) if t.startswith("[AGENT_HISTORY]")
        ]
        self.assertEqual(1, len(journal))
        self.assertIn("<step 1>", journal[0])
        self.assertNotIn("<step 2>", journal[0])
        self.assertEqual(
            1, sum(1 for m in msgs if isinstance(m, AssistantMessage))
        )
        texts3 = _texts(msgs)
        state_idxs = [
            i for i, t in enumerate(texts3) if t.startswith("[PAGE_STATE]")
        ]
        self.assertEqual(1, len(state_idxs))
        # Pre-call nudges (here: [STAGNATION] — the fake page never
        # changes) may follow the state message; nothing else can.
        for m in msgs[state_idxs[0] + 1 :]:
            self.assertIsInstance(m, UserMessage)
        # Persistent <memory> from the model's prior turns is re-injected.
        state_text = texts3[state_idxs[0]]
        self.assertIn("<agent_state>", state_text)
        self.assertIn("step 2 noted", state_text)

        # The run finished via the plain-text path.
        results = agent.history.history[-1].result
        self.assertTrue(any(r.is_done for r in results))

    def test_post_call_nudge_survives_exactly_one_rebuild(self):
        llm = ScriptedToolChoiceLLM(
            [
                ChatInvokeCompletion(text="", tool_calls=[]),
                ChatInvokeCompletion(text="", tool_calls=[]),
                ChatInvokeCompletion(text="the result is 42"),
            ]
        )
        agent = _make_agent(llm)
        asyncio.run(agent.run())

        self.assertEqual(3, len(llm.calls))
        # Call 1: no forcing yet.
        self.assertIsNone(llm.calls[0]["tool_choice"])
        # Calls 2 and 3: the EMPTY_MODEL_OUTPUT path forces "required".
        self.assertEqual("required", llm.calls[1]["tool_choice"])
        self.assertEqual("required", llm.calls[2]["tool_choice"])

        # Each rebuild carries only the LATEST empty-output nudge.
        for call in (llm.calls[1], llm.calls[2]):
            nudges = [
                t
                for t in _texts(call["messages"])
                if t.startswith("[EMPTY_MODEL_OUTPUT]")
            ]
            self.assertEqual(1, len(nudges))

    def test_keep_native_zero_is_pure_re_render(self):
        llm = ScriptedLLM(
            [
                _poke_call(1),
                ChatInvokeCompletion(text="the result is 42"),
            ]
        )
        agent = _make_agent(llm, render_keep_native_turns=0)
        asyncio.run(agent.run())

        msgs = llm.calls[1]["messages"]
        self.assertFalse(
            any(
                isinstance(m, (AssistantMessage, ToolResultMessage))
                for m in msgs
            )
        )
        last_turn = [
            t
            for t in _texts(msgs)
            if t.startswith("[LAST_TURN_RESULTS]")
        ]
        self.assertEqual(1, len(last_turn))
        self.assertIn("poke ok", last_turn[0])
        # With no native turn kept, step 1 is in the journal too.
        journal = [
            t for t in _texts(msgs) if t.startswith("[AGENT_HISTORY]")
        ]
        self.assertEqual(1, len(journal))
        self.assertIn("<step 1>", journal[0])

    def test_last_step_forces_done_tool_choice(self):
        llm = ScriptedToolChoiceLLM(
            [
                ChatInvokeCompletion(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id="d1",
                            name="done",
                            args={"text": "forced answer", "success": False},
                        )
                    ],
                ),
            ]
        )
        agent = _make_agent(llm, max_steps=1)
        asyncio.run(agent.run())

        self.assertEqual(1, len(llm.calls))
        self.assertEqual({"name": "done"}, llm.calls[0]["tool_choice"])
        final_turn = [
            t
            for t in _texts(llm.calls[0]["messages"])
            if t.startswith("[FINAL TURN]")
        ]
        self.assertEqual(1, len(final_turn))
        self.assertIn("done(text=", final_turn[0])
        results = agent.history.history[-1].result
        self.assertTrue(
            any(
                r.is_done and r.extracted_content == "forced answer"
                for r in results
            )
        )

    def test_force_final_answer_reads_forced_done_args(self):
        llm = ScriptedToolChoiceLLM(
            [
                _poke_call(1),
                ChatInvokeCompletion(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id="d2",
                            name="done",
                            args={"text": "best partial", "success": False},
                        )
                    ],
                ),
            ]
        )
        # max_steps=1 and call 1 ignores the forced done → the loop
        # exhausts and _force_final_answer runs as call 2.
        agent = _make_agent(llm, max_steps=1)
        asyncio.run(agent.run())

        self.assertEqual(2, len(llm.calls))
        self.assertEqual({"name": "done"}, llm.calls[1]["tool_choice"])
        results = agent.history.history[-1].result
        self.assertTrue(
            any(
                r.is_done and r.extracted_content == "best partial"
                for r in results
            )
        )

    def test_render_mode_skips_durable_first_read_bypass(self):
        # In render mode a ToolResultMessage survives one rebuild, so
        # the v0.11.5 "first large read stays durable" bypass would
        # silently lose the content. Render mode must route the first
        # large read through the read_state lifecycle instead.
        from browser_use_rs.agent import EPHEMERAL_RESULT_THRESHOLD
        from browser_use_rs.llm.base import TextPart

        big = "x" * (EPHEMERAL_RESULT_THRESHOLD + 10)

        render_agent = _make_agent(ScriptedLLM([]))
        parts, summary = render_agent._apply_ephemeral_lifecycle(
            "page_text", [TextPart(text=big)], big
        )
        self.assertTrue(summary.startswith("[Result from page_text"))
        self.assertEqual(1, len(render_agent._read_state_for_next_turn))

        transcript_agent = _make_agent(
            ScriptedLLM([]), context_mode="transcript"
        )
        parts, summary = transcript_agent._apply_ephemeral_lifecycle(
            "page_text", [TextPart(text=big)], big
        )
        self.assertEqual(big, summary)  # durable bypass still applies
        self.assertEqual(0, len(transcript_agent._read_state_for_next_turn))

    def test_render_mode_retains_small_read_results(self):
        # v0.12.18: sub-25KB extract/search results previously fell
        # through both retention nets in render mode (not native beyond
        # one rebuild, below the read_state threshold) — the task-361
        # re-fetch-loop mechanism. Render mode now routes essentially
        # every read result through <read_state> + spill file.
        from browser_use_rs.llm.base import TextPart

        small = "1. Digital Sky 2. Digital art 3. Summer Lushness I" * 10

        render_agent = _make_agent(ScriptedLLM([]))
        parts, summary = render_agent._apply_ephemeral_lifecycle(
            "extract_result_cards", [TextPart(text=small)], small
        )
        self.assertTrue(summary.startswith("[Result from extract_result_cards"))
        queue = render_agent._read_state_for_next_turn
        self.assertEqual(1, len(queue))
        self.assertEqual(small, queue[0]["content"])

        # Transcript mode keeps the old contract: small reads stay
        # inline (they remain native forever there) and the extract
        # tools are not lifecycle-managed at all.
        transcript_agent = _make_agent(
            ScriptedLLM([]), context_mode="transcript"
        )
        parts, summary = transcript_agent._apply_ephemeral_lifecycle(
            "extract_result_cards", [TextPart(text=small)], small
        )
        self.assertEqual(small, summary)
        self.assertEqual(0, len(transcript_agent._read_state_for_next_turn))

    def test_transcript_mode_escape_hatch(self):
        llm = ScriptedLLM([ChatInvokeCompletion(text="the result is 42")])
        agent = _make_agent(llm, context_mode="transcript")
        self.assertEqual("transcript", agent.context_mode)
        asyncio.run(agent.run())
        # Transcript mode still finishes and keeps the task at index 0.
        self.assertIn(
            "<user_request>", _msg_text(llm.calls[0]["messages"][0])
        )


class ToolChoiceMappingTests(unittest.TestCase):
    def test_anthropic_mapping(self):
        try:
            from browser_use_rs.llm.anthropic import ChatAnthropic
        except ImportError:
            self.skipTest("anthropic SDK not installed")
        m = ChatAnthropic._map_tool_choice
        self.assertIsNone(m(None))
        self.assertIsNone(m("auto"))
        self.assertEqual({"type": "any"}, m("required"))
        self.assertEqual({"type": "none"}, m("none"))
        self.assertEqual(
            {"type": "tool", "name": "done"}, m({"name": "done"})
        )
        self.assertIsNone(m("bogus"))

    def test_openai_mapping(self):
        try:
            from browser_use_rs.llm.openai import ChatOpenAI
        except ImportError:
            self.skipTest("openai SDK not installed")
        m = ChatOpenAI._map_tool_choice
        self.assertIsNone(m(None))
        self.assertEqual("required", m("required"))
        self.assertEqual("none", m("none"))
        self.assertEqual(
            {"type": "function", "function": {"name": "done"}},
            m({"name": "done"}),
        )

    def test_google_mapping(self):
        try:
            from browser_use_rs.llm.google import ChatGoogle
        except ImportError:
            self.skipTest("google-genai SDK not installed")
        tc = ChatGoogle._map_tool_choice({"name": "done"})
        fcc = tc.function_calling_config
        self.assertEqual(["done"], list(fcc.allowed_function_names))
        self.assertIsNone(ChatGoogle._map_tool_choice("auto"))


if __name__ == "__main__":
    unittest.main()


class ResponsesApiMappingTests(unittest.TestCase):
    def test_responses_input_mapping(self):
        try:
            from browser_use_rs.llm.openai import ChatOpenAI, _to_responses_input
        except ImportError:
            self.skipTest("openai SDK not installed")
        from browser_use_rs.llm.base import ImagePart, TextPart

        msgs = [
            UserMessage(content="do the task"),
            AssistantMessage(
                text="<memory>x</memory>",
                tool_calls=[ToolCall(id="c1", name="click", args={"index": 3})],
            ),
            ToolResultMessage(
                tool_call_id="c1",
                name="click",
                content=[TextPart(text="clicked"), ImagePart(data="QUJD", media_type="image/png")],
            ),
            UserMessage(content=[TextPart(text="[PAGE_STATE]"), ImagePart(data="REVG")]),
        ]
        items = _to_responses_input(msgs)
        kinds = [i.get("type") or i.get("role") for i in items]
        # user, assistant text, function_call, function_call_output,
        # image flush user, page-state user
        self.assertEqual(
            ["user", "assistant", "function_call", "function_call_output", "user", "user"],
            kinds,
        )
        fc = items[2]
        self.assertEqual("c1", fc["call_id"])
        self.assertIn('"index": 3', fc["arguments"])
        self.assertEqual("clicked", items[3]["output"])
        flush = items[4]["content"][0]
        self.assertEqual("input_image", flush["type"])
        self.assertTrue(flush["image_url"].startswith("data:image/png;base64,"))
        state_parts = items[5]["content"]
        self.assertEqual("input_text", state_parts[0]["type"])
        self.assertEqual("input_image", state_parts[1]["type"])

        m = ChatOpenAI._map_responses_tool_choice
        self.assertEqual({"type": "function", "name": "done"}, m({"name": "done"}))
        self.assertEqual("required", m("required"))
        self.assertIsNone(m("auto"))
