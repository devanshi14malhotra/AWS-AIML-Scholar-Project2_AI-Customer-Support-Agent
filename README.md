# Customer Support AI Agent (Amazon Bedrock AgentCore)

**Submitted by Devanshi Malhotra**

Udacity Future AWS Agent Engineer, Project 2

A customer support agent for a fictional e-commerce store, built with the Strands SDK and deployed to Amazon Bedrock AgentCore Runtime. It tracks orders, processes refunds, answers policy questions from a knowledge base, remembers customers across sessions, calculates loyalty discounts, and can open web pages.

## What's in this repo

```
.
├── README.md
├── reflections.md            written reflection
├── submission-screenshots/   terminal output for the 6 tests
└── starter/
    ├── main.py               the agent (all sections implemented)
    ├── requirements.txt      dependencies used for the deploy
    ├── pyproject.toml
    ├── product_catalog.txt   data for the knowledge base
    └── lambda/               order_tracker.py, refund_processor.py, lambda_schema
```

## How it's put together

- **Agent:** Strands `Agent` with Amazon Nova 2 Lite, served through `BedrockAgentCoreApp`. Deployed with the starter toolkit (`agentcore configure` and `agentcore deploy`), Direct Code Deploy, Python 3.13.
- **Order and refund tools:** two Lambda functions behind an AgentCore Gateway (MCP, no authorizer, as the project asks). Order lookups go through a REST API on API Gateway (`get_order`, `get_customer_orders`, `get_customer`). Refunds call the Lambda directly with an inline tool schema.
- **Knowledge base:** `search_knowledge_base` calls the Bedrock Retrieve API over the product catalog in S3.
- **Memory:** `MemoryHook` adds relevant memories to each customer message before the model sees it, and saves each finished turn afterwards. It uses two strategies, semantic (`customer_facts`) and user preference (`customer_preferences`).
- **Loyalty discounts:** `calculate_loyalty_discount` runs the arithmetic as Python in the AgentCore Code Interpreter, with a simple fallback if the sandbox is unavailable.
- **Browser:** the AgentCore Browser tool, driven through Playwright.

## Test results

| # | Test | Result |
|---|------|--------|
| 1 | Track order ORD-001 | SHIPPED, UPS, tracking TRK987654321, estimated delivery given |
| 2 | Refund for ORD-002 | Refund ID returned, status APPROVED, 3-5 business days |
| 3 | Platinum tier benefits | Free same-day shipping, 15% discount, priority support (from the knowledge base) |
| 4 | Memory across sessions | Session C recalled "Jane" and the preference for concise replies |
| 5 | Gold member, 4250 points, $150 order | 4000 points redeemed ($40), 10% tier discount ($11), total $99, 250 points left |
| 6 | Page title of udacity.com | Returned "Learn the Latest Tech Skills; Advance Your Career \| Udacity" |

Screenshots are in `submission-screenshots/`. In Test 4, the first recall attempt (session B) came too soon after the introduction, before memory extraction had finished, so it didn't remember. The second attempt (session C) did.

## Things that differ from the setup instructions

- **Knowledge base type:** the sandbox account was denied OpenSearch Serverless and S3 Vectors, so the knowledge base is a Bedrock Managed KB over the same S3 catalog instead of Titan v2 with OpenSearch.
- **Dependencies:** the deploy uses `requirements.txt` because the resolved `pyproject.toml` pulled in `pywin32`, which can't be built for the Linux runtime.
- **Playwright fix:** a zip built on Windows drops the executable bit on Playwright's `node` binary, which broke the browser tool. `main.py` restores it at startup (`_fix_playwright_driver`).
- **Permissions:** the agent's execution role was given broad `bedrock-agentcore:*` access to get past lab AccessDenied errors. That is fine for a throwaway lab account and not something I'd keep in production (see `reflections.md`).

## Running it

Fill in `GATEWAY_URL`, `KB_ID` and `MEMORY_ID` at the top of `starter/main.py`, then from `starter/`:

```
uv run agentcore configure --entrypoint main.py --name csai_agent
uv run agentcore deploy
uv run agentcore invoke '{"prompt": "Can you track order ORD-001?", "customer_id": "CUST-123", "session_id": "t1"}'
```

The AWS resources used for testing were deleted after submission, so the URL and IDs in `main.py` no longer point to anything.