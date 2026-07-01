# Add a New Model Backend

SkillOpt supports multiple LLM backends. This guide shows how to add your own.

## Backend Architecture

```
skillopt/model/
├── base.py           # Abstract base class
├── azure_openai.py   # Azure OpenAI backend
├── openai_model.py   # Direct OpenAI backend
├── claude.py         # Anthropic Claude backend
├── qwen.py           # Local Qwen (vLLM) backend
└── your_backend.py   # Your new backend
```

## Step 1: Create the Backend

Create `skillopt/model/your_backend.py`:

```python
from skillopt.model.base import ModelBackend, ModelResponse

class YourBackend(ModelBackend):
    """Your custom model backend."""
    
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.model_name = cfg.get('model_name', 'your-default-model')
        self.api_key = os.environ.get('YOUR_API_KEY', '')
        self.client = self._init_client()
    
    def _init_client(self):
        """Initialize API client."""
        # TODO: Set up your API client
        pass
    
    async def generate(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs
    ) -> ModelResponse:
        """
        Generate a completion.
        
        Args:
            messages: Chat messages [{"role": "...", "content": "..."}]
            temperature: Sampling temperature
            max_tokens: Maximum tokens in response
            
        Returns:
            ModelResponse with content, usage, and metadata
        """
        response = await self.client.chat(
            model=self.model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        
        return ModelResponse(
            content=response.text,
            usage={
                'prompt_tokens': response.usage.input,
                'completion_tokens': response.usage.output,
            },
            model=self.model_name,
        )
    
    async def generate_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        **kwargs
    ) -> ModelResponse:
        """Generate with tool/function calling support."""
        # Optional: implement if your model supports tool use
        raise NotImplementedError("Tool use not supported")
```

## Step 2: Register the Backend

Add to `skillopt/model/__init__.py`:

```python
from .your_backend import YourBackend

BACKEND_REGISTRY = {
    # ... existing backends ...
    'your_backend': YourBackend,
}
```

## Step 3: Configure

Use your backend in any config:

```yaml
model:
  backend: your_backend
  model_name: your-model-id
  temperature: 0.7
  max_tokens: 4096
```

Set credentials via environment variable:

```bash
export YOUR_API_KEY="your-key"
```

## Required Interface

Your backend must implement these methods:

| Method | Required | Description |
|---|---|---|
| `generate()` | ✅ | Basic text generation |
| `generate_with_tools()` | Optional | Tool/function calling |
| `count_tokens()` | Optional | Token counting for context management |

## Tips

!!! tip
    - Test your backend with `python -c "from skillopt.model.your_backend import YourBackend"` first
    - Use `async` methods for all API calls — SkillOpt uses asyncio throughout
    - Implement retry logic with exponential backoff for production use
    - Add your API key to `.env.example` when submitting a PR
