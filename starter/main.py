"""
Customer Support AI Agent
==========================
E-commerce support agent built with Strands + Amazon Bedrock AgentCore.

Capabilities:
  - Order tracking and refunds via AgentCore Gateway (MCP)
  - Product / policy questions via a Bedrock Knowledge Base (RAG)
  - Cross-session memory via AgentCore Memory
  - Exact loyalty discount math via AgentCore Code Interpreter
  - Live web browsing via AgentCore Browser

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")


def _fix_playwright_driver():
    """
    Direct Code Deploy zips built on Windows drop the executable bit on
    Playwright's bundled node binary, so the Browser tool fails with
    "PermissionError: [Errno 13] Permission denied: .../driver/node".
    Restore the bit at startup; if the package dir is read-only, run a
    copy from /tmp instead.
    """
    try:
        import playwright
        import shutil
        import stat

        node_src = os.path.join(os.path.dirname(playwright.__file__), "driver", "node")
        if not os.path.exists(node_src) or os.access(node_src, os.X_OK):
            return
        try:
            mode = os.stat(node_src).st_mode
            os.chmod(node_src, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            node_copy = "/tmp/playwright-node"
            shutil.copy2(node_src, node_copy)
            os.chmod(node_copy, 0o755)
            os.environ["PLAYWRIGHT_NODEJS_PATH"] = node_copy
    except Exception as e:
        logger.error(f"Could not fix Playwright driver permissions: {e}")


_fix_playwright_driver()

# ── 1. App initialisation ─────────────────────────────────────────────────────
# Registers the ASGI server used by AgentCore Runtime. One instance per deployment.
app = BedrockAgentCoreApp()


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── 2. Configuration ──────────────────────────────────────────────────────────
GATEWAY_URL = "https://customersupportgateway-ax5pl2oj9h.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"  # your Gateway URL
KB_ID       = "3QHOJIOTA0"   # 10-char Knowledge Base ID
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-CWek3n5zOn"           # AgentCore Memory ID


# ── 3. Model and clients ──────────────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"

model = BedrockModel(model_id=model_id, region_name=REGION)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── 4. Namespace helper ───────────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    return {
        s["type"]: s["namespaces"][0]
        for s in strategies
        if s.get("namespaces")
    }


# ── 5. Memory hook ────────────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        self.namespaces = get_namespaces(memory_client, memory_id)
        # Original user text (before memory context is prepended), so we
        # don't store the injected "Customer Context" block back into memory.
        self._original_query = ""

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return

        last = messages[-1]
        content = last.get("content", [])

        # Only act on plain-text user messages (skip assistant msgs and tool results)
        if last.get("role") != "user" or not content:
            return
        if any(isinstance(b, dict) and "toolResult" in b for b in content):
            return
        if "text" not in content[0]:
            return

        user_query = content[0]["text"]
        self._original_query = user_query

        try:
            all_context = []
            for strategy_type, ns_template in self.namespaces.items():
                namespace = ns_template.format(actorId=self.actor_id)
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=user_query,
                    top_k=5,
                )
                for m in memories:
                    if not isinstance(m, dict):
                        continue
                    text = (m.get("content", {}) or {}).get("text", "").strip()
                    if text:
                        all_context.append(f"[{strategy_type}] {text}")

            if all_context:
                context_text = "\n".join(all_context)
                content[0]["text"] = f"Customer Context:\n{context_text}\n\n{user_query}"
        except Exception as e:
            # Memory should never break the conversation
            logger.error(f"Memory retrieval failed: {e}")

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        try:
            messages = event.agent.messages
            agent_response = ""
            customer_query = ""

            # Walk backwards: last assistant text first, then the user query before it
            for msg in reversed(messages):
                role = msg.get("role")
                content = msg.get("content", [])
                texts = [b["text"] for b in content if isinstance(b, dict) and "text" in b]
                has_tool_result = any(
                    isinstance(b, dict) and "toolResult" in b for b in content
                )

                if not agent_response:
                    if role == "assistant" and texts:
                        agent_response = " ".join(texts)
                elif role == "user" and texts and not has_tool_result:
                    customer_query = texts[0]
                    break

            # Prefer the original query so injected context isn't saved
            customer_query = self._original_query or customer_query

            if customer_query and agent_response:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[
                        (customer_query, "USER"),
                        (agent_response, "ASSISTANT"),
                    ],
                )
        except Exception as e:
            logger.error(f"Memory save failed: {e}")

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── 6. Knowledge Base tool ────────────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID:
        return "Knowledge base not configured."

    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
        results = resp.get("retrievalResults", [])
        if not results:
            return "No relevant information found in the knowledge base."

        chunks = [
            r["content"]["text"]
            for r in results
            if r.get("content", {}).get("text")
        ]
        if not chunks:
            return "No relevant information found in the knowledge base."
        return "\n---\n".join(chunks)
    except Exception as e:
        logger.error(f"Knowledge base search failed: {e}")
        return f"Knowledge base search failed: {e}"


# ── 7. Loyalty discount tool (Code Interpreter) ───────────────────────────────
@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    tier = tier.strip().title()
    product_category = product_category.strip().lower()

    # Self-contained script that runs inside the sandbox.
    # Rules: 100 points = $1, redeem in blocks of 500, cap at 50% of the order,
    # then apply the tier discount to the subtotal after points.
    code = f'''
import json

loyalty_points = {int(loyalty_points)}
tier = "{tier}"
order_total = {float(order_total)}
category = "{product_category}"

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

# Points: floor to nearest 500, capped at 50% of the order value (100 pts = $1)
max_points_for_order = int(order_total * 0.5 * 100)
points_redeemed = min(
    (loyalty_points // 500) * 500,
    (max_points_for_order // 500) * 500,
)
points_value = points_redeemed / 100

subtotal_after_points = order_total - points_value
tier_discount_rate = tier_rates.get(tier, 0.0)
tier_discount = round(subtotal_after_points * tier_discount_rate, 2)

final_total = round(subtotal_after_points - tier_discount, 2)
total_savings = round(order_total - final_total, 2)
points_earned = int(final_total * earn_rates.get(category, 1))
remaining_points = loyalty_points - points_redeemed

print(json.dumps({{
    "order_total": order_total,
    "tier": tier,
    "points_redeemed": points_redeemed,
    "points_value": points_value,
    "subtotal_after_points": round(subtotal_after_points, 2),
    "tier_discount_rate": tier_discount_rate,
    "tier_discount": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
    "new_balance_after_earning": remaining_points + points_earned,
}}))
'''

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )
            for evt in response["stream"]:
                return json.dumps(evt["result"])
        return "Code Interpreter returned no result."

    except Exception as e:
        # Fallback: tier discount only (no points redemption)
        logger.error(f"Code Interpreter unavailable, using fallback: {e}")
        rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        rate = rates.get(tier, 0.0)
        discount = round(order_total * rate, 2)
        return json.dumps({
            "note": "Code Interpreter unavailable - tier discount only, points not applied.",
            "order_total": order_total,
            "tier": tier,
            "tier_discount_rate": rate,
            "tier_discount": discount,
            "final_total": round(order_total - discount, 2),
        })


# ── 8. Agent entrypoint ───────────────────────────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """You are a friendly, efficient customer support assistant for an Amazon-style store.

The current customer's ID is {actor_id}.

Tools and when to use them:
- Order tools (from the gateway): look up order status, tracking, and customer orders. Use the customer ID above when a customer ID is needed.
- Refund tools (from the gateway): before initiating a refund, look up the order to get the item and amount, then call initiate_refund with order_id, reason and amount. Report the refund ID, status, and timeline from the result.
- search_knowledge_base: product specs, return/refund policy, warranty, loyalty program details, order status definitions. Always use it for policy questions instead of guessing.
- calculate_loyalty_discount: any loyalty points / tier discount calculation. Never do this math yourself.
- browser: only when the customer asks about live web content. Navigate to the URL and report what you find.

Customer Context (if present at the start of a message) contains remembered facts and preferences about this customer. Use it naturally, including their name and communication preferences, and don't mention that it was retrieved.

Be accurate. If a tool fails or information is missing, say so instead of making things up."""


@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id", "guest")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)
        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        # Connect to the Gateway (MCP) and add its tools
        gateway_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
        with gateway_client:
            gateway_tools = gateway_client.list_tools_sync()
            tools.extend(gateway_tools)

            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=SYSTEM_PROMPT_TEMPLATE.format(actor_id=actor_id),
            )
            response = agent(user_input)

        # Return the first text block of the final message
        for block in response.message.get("content", []):
            if "text" in block:
                return block["text"]
        return str(response)

    except Exception as e:
        logger.error(f"Agent invocation failed: {e}", exc_info=True)
        return f"Sorry, something went wrong while handling your request: {e}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()