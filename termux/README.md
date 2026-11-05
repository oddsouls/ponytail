# Ponytail Mobile: Termux + OpenCode + free API failover

This optional runtime profile turns Ponytail's existing OpenCode adapter into a
phone-first coding setup:

**Android → Termux → Ubuntu proot → OpenCode + Ponytail → loopback gateway → free APIs**

Ponytail itself stays an instruction/plugin layer. The mobile profile adds no
provider logic to the Ponytail skill: OpenCode sees one local model,
`ponytail-free/free`, while the gateway on `127.0.0.1:8766` handles free-provider
routing and health.

## Install

Use the official F-Droid Termux build. From a Ponytail checkout:

```bash
bash termux/install.sh
ptmobile key gemini
ptmobile key groq
ptmobile key openrouter
ptmobile doctor
ptmobile status
ptmobile init myproject
```

You only need one provider key. Independent providers make the setup much more
resilient when a free quota or model disappears.

Commands:

```text
ptmobile key gemini|groq|openrouter   store/update a key without echoing it
ptmobile rotate-token                rotate the localhost-only bearer token
ptmobile start / stop                manage the localhost gateway
ptmobile status                       routes, latency, cooldowns, rate headers
ptmobile discover                     refresh model catalogs; no generation call
ptmobile doctor                       validate Termux, Ubuntu, OpenCode and gateway
ptmobile open [project]               open OpenCode in Ubuntu
ptmobile init NAME                    create git project + open it
ptmobile logs                         tail gateway log
```

`ptmobile open` holds a Termux wake lock while the OpenCode TUI is running and
releases it when the session exits. This is useful when Android turns the screen
off during a long agent turn.

## Free providers

The defaults are deliberately conservative and current as of **2026-11-05**:

- **Gemini Developer API** through Google's OpenAI-compatible endpoint:
  `gemini-3.8-flash`, `gemini-3.6-flash`, then `gemini-3.1-flash-lite`.
  Google's pricing page currently marks input and output for those models as
  free of charge on the Free Tier. Use a Free Tier project/key if zero spend is
  required; a paid project is a different billing context.
- **GroqCloud Free tier**: `openai/gpt-oss-120b`, `qwen/qwen3.8-27b`, then
  `openai/gpt-oss-20b`. Groq currently publishes Free-tier limits of 30 RPM,
  1,000 RPD, 8K TPM and 200K TPD for each of these models. Free-tier accounts
  are distinct from the billed Developer tier.
- **OpenRouter** defaults to `openrouter/free`. Discovery may add only explicit
  model IDs ending in `:free`; it never auto-selects paid IDs. OpenRouter's
  current free-variant cap is 20 RPM and 50 requests/day (higher after account
  credit purchases), so it is deliberately the last default provider.

Provider offerings change. `ptmobile discover` reads `/models` catalogs and
never sends a generation request. Runtime availability is learned from real
coding requests rather than spending free quota on health probes.

## Reliability rules

- configured provider priority is the starting order;
- successful routes gain a small health score; repeated failures lose score;
- 429/quota → long cooldown or the provider's `Retry-After` value;
- 401/auth → provider-wide cooldown, so one bad key does not burn every model;
- missing model/404 → 24-hour model cooldown;
- 5xx/network timeout → short exponential cooldown;
- known API incompatibility → six-hour route cooldown;
- other 4xx errors are returned immediately: failover must not hide a malformed
  request, bad tool schema, or project-level failure;
- successful rate-limit headers are retained and shown by `ptmobile status`;
- only two model requests run concurrently by default (`maxConcurrent: 2`) to
  avoid accidentally exhausting a phone/free account; configurable 1..8;
- route state is bounded and atomically written;
- the gateway binds only to loopback and uses constant-time bearer-token checks;
- streaming is never replayed after bytes have reached OpenCode, because doing
  so could duplicate text or tool calls.

## Secrets

Provider keys are stored in:

```text
~/.config/ponytail/mobile-secrets.json
```

The file is written atomically with mode `0600`; the gateway refuses a secrets
file that is group/world-readable. It is parsed as JSON, not executed with
`source`.

Upstream provider keys **never enter the Ubuntu proot**. Only a random localhost
gateway token is copied to `/root/.config/ponytail/gateway.token` (0600).
OpenCode reads that token with its `{file:...}` secret reference.

## OpenCode integration

The installer merges the current OpenCode config instead of replacing it and
keeps a one-time backup at:

```text
/root/.config/opencode/opencode.pre-ponytail-mobile.json
```

It refuses to overwrite malformed existing JSON, keeps a one-time backup, and
detects the installed OpenCode major version before editing config. OpenCode
2 currently uses `plugins` + `providers` with the
`@opencode/ai/providers/openai-compatible` runtime, while OpenCode 1 uses
`plugin` + `provider` with `@ai-sdk/openai-compatible`. The installer writes the
matching shape, points it at `http://127.0.0.1:8766/v1`, and selects
`ponytail-free/free`. This keeps the mobile profile usable across both plugin
APIs that Ponytail already supports.

## Files

- `free_gateway.py` — stdlib-only OpenAI-compatible gateway/router.
- `configure_opencode.py` — version-aware V1/V2 config merger, separately testable.
- `mobile.example.json` — provider/model priorities and concurrency; preserved
  across reinstalls after its first copy.
- `ptmobile` — phone-friendly management/diagnostics command.
- `install.sh` — idempotent Termux/Ubuntu/OpenCode installer and config merger.
