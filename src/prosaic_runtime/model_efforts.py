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

# Gemma 4 has a native thinking switch, not distinct effort budgets.
# https://ai.google.dev/gemma/docs/capabilities/thinking
# Exact community aliases below were verified against their served templates.
GEMMA_TOGGLE_MODELS = {
    alias for model in ('gemma-4-e2b-it', 'gemma-4-e4b-it', 'gemma-4-12b-it',
                       'gemma-4-26b-a4b-it', 'gemma-4-31b-it')
    for alias in (model, 'google/' + model)
} | {'gemma-4-31b-it-oq4e-mtp', 'jundot/gemma-4-31b-it-oq4e-mtp'}
QWEN_TEMPLATE_MODELS = {
    'thinkingcap-qwen3.8-27b-mlx-4bit-dwq',
    'bottlecapai/thinkingcap-qwen3.8-27b-mlx-4bit-dwq',
}
# These documented APIs also accept none as an off control. Keep it separate
# from their positive effort tiers; unknown models require an explicit assertion.
# https://api-docs.deepseek.com/api/list-models/
# https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions
API_OFF_MODELS = {'deepseek-flash', 'deepseek-v4-pro', 'qwen3.8-max',
                  'qwen3.8-max-0902', 'qwen3.8-flash', 'qwen3.8-2.4t-a95b', 'qwen3.8-27b'}


def automatic_effort_format(model):
    if model.lower() in GEMMA_TOGGLE_MODELS:
        return 'thinking_toggle'
    if model.lower() in QWEN_TEMPLATE_MODELS:
        return 'chat_template_effort'
    return 'reasoning_effort'


def automatic_efforts(provider, model):
    if provider == 'openai-compatible':
        if model.lower() in GEMMA_TOGGLE_MODELS:
            return ('none', *STANDARD)
        if model.lower() in QWEN_TEMPLATE_MODELS:
            return ('low', 'medium', 'xhigh')
    return {'openai-compatible': API_MODELS, 'codex-cli': CODEX_MODELS,
            'claude-code': CLAUDE_MODELS}.get(provider, {}).get(model)
