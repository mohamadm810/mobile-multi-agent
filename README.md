# Mobile Multi-Agent

A phone-manageable, GitHub Actions-powered multi-agent project planning team.
The first release is deliberately planning-only: it does not execute shell commands,
edit external project repositories, or deploy anything.

See [README_FA.md](README_FA.md) for Android-first setup instructions.


## Independent model-provider fallback

Gemini remains the primary provider. To enable a genuinely independent fallback, add
the repository Actions secret `OPENAI_API_KEY`. The workflow uses `gpt-4.1-mini`
by default; optionally set the Actions variables `OPENAI_MODEL` and
`OPENAI_BASE_URL` for a different OpenAI-compatible deployment. The base URL must
use HTTPS and must not contain embedded credentials. If no second-provider secret is
configured, the agent continues to use Gemini model fallbacks and checkpoint recovery;
it does not pretend an independent provider is available.
