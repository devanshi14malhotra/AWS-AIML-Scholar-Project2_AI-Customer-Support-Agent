# Reflection

**Design decision: what gets saved to memory**

The MemoryHook prepends retrieved memories to the customer's message before the model sees it. My first idea was to save whatever the agent saw, but that would write the injected "Customer Context" block back into memory on every turn, so memories would slowly start quoting themselves. Instead, the hook keeps the customer's original text and saves that plus the agent's final reply. It's a small change, but it keeps long-term memory limited to what the customer actually said.

**Challenge: the browser tool failing after deploy**

Tests 1-5 passed, but Test 6 kept returning a vague "problem initializing the browser" message. The CLI couldn't show logs (the lab account denies it), so I read the runtime log in the CloudWatch console and found `PermissionError: Permission denied: .../playwright/driver/node`. The cause was deploying from Windows: the zip lost the executable bit on Playwright's bundled node binary. I first confirmed the account itself could start browser sessions, then added a small startup function that restores the bit (with a /tmp copy as a fallback). After redeploying, the agent returned the udacity.com page title. The lesson: the agent's own reply can hide the real error, so read the logs first.

**Production consideration: access control**

To get past AccessDenied errors in the lab, I gave the agent's execution role broad `bedrock-agentcore:*` permissions, and the Gateway uses the NONE authorizer as the project requires. Neither would be acceptable in production. I'd scope the role to the specific actions and resources it uses, and put IAM or JWT authorization on the Gateway, since right now anyone with the URL can call the tools.

**Note on the setup:** the sandbox blocked OpenSearch Serverless and S3 Vectors (explicit permission denials), so the Knowledge Base uses a Bedrock Managed KB over the same S3 catalog instead of Titan v2 with OpenSearch.