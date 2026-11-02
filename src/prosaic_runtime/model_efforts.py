"""Documented offline defaults when a provider has no effort metadata API.

Snapshot: 2026-11-02. Native CLI advertisements take precedence. These exact
identities are deliberately not prefix guesses for future/custom models.
Sources: OpenAI model pages and Codex Models; Claude Code model-config.
"""

STANDARD = ('low', 'medium', 'high', 'xhigh', 'max')
CODEX_ULTRA = STANDARD + ('ultra',)
OPENAI_MODELS = {
    name: STANDARD for name in (
        'gpt-6-astra', 'gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-sol', 'gpt-6-luna',
        'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')}
# Exact documented Chat Completions identities, not family-prefix inference.
# https://api-docs.deepseek.com/api/list-models/
# https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions
# https://docs.nvidia.com/nim/large-language-models/2.0.13/get-started/advanced/get-started-nemotron-3.5-super-vl.html
API_MODELS = {
    **OPENAI_MODELS,
    **{name: ('low', 'high', 'max') for name in ('deepseek-flash', 'deepseek-v4-pro')},
    **{name: ('low', 'medium', 'xhigh') for name in (
        'qwen3.8-max', 'qwen3.8-max-0902', 'qwen3.8-flash',
        'qwen3.8-2.4t-a95b', 'qwen3.8-27b')},
    'nvidia/nemotron-3.5-super-vl-120b-a12b': ('low', 'medium', 'high'),
}
CODEX_MODELS = {
    **OPENAI_MODELS,
    **{name: CODEX_ULTRA for name in (
        'gpt-6-astra', 'gpt-6.1-sol', 'gpt-6.1-terra', 'gpt-6-sol',
        'gpt-5.6-sol', 'gpt-5.6-terra')},
}
CLAUDE_MODELS = {
    name: STANDARD for name in (
        'claude-opus-5-5', 'claude-sonnet-5-5',
        'claude-haiku-5-5', 'claude-opus-5', 'claude-sonnet-5',
        'claude-opus-4-8', 'claude-opus-4-7', 'claude-fable-5-1', 'claude-fable-5')}
CLAUDE_MODELS.update({name: ('low', 'medium', 'high', 'max')
                      for name in ('claude-opus-4-6', 'claude-sonnet-4-6')})


def automatic_efforts(provider, model):
    return {'openai-compatible': API_MODELS, 'codex-cli': CODEX_MODELS,
            'claude-code': CLAUDE_MODELS}.get(provider, {}).get(model)
