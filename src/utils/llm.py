"""Helper functions for LLM"""

import json
import time
import threading
from pydantic import BaseModel
from src.llm.models import get_model, get_model_info
from src.utils.progress import progress
from src.graph.state import AgentState


# Global rate limiter for Google Gemini free tier
class RateLimiter:
    def __init__(self):
        self.last_call_time = {}
        self.call_count = {}
        self.lock = threading.Lock()
    
    def wait_if_needed(self, model_provider: str, model_name: str):
        """Add delay for Google Gemini free tier rate limits"""
        if model_provider.upper() != "GOOGLE":
            return
            
        with self.lock:
            now = time.time()
            key = f"{model_provider}_{model_name}"
            
            # Reset counters every minute
            if key not in self.last_call_time or (now - self.last_call_time.get(f"{key}_reset", 0)) > 60:
                self.call_count[key] = 0
                self.last_call_time[f"{key}_reset"] = now
            
            # Check if we've hit the rate limit (10 requests per minute for free tier)
            if self.call_count.get(key, 0) >= 9:  # Leave some buffer
                time_since_reset = now - self.last_call_time.get(f"{key}_reset", 0)
                if time_since_reset < 60:
                    sleep_time = 60 - time_since_reset + 1  # Add 1 second buffer
                    print(f"🕒 Google Gemini rate limit reached. Waiting {sleep_time:.1f}s...")
                    time.sleep(sleep_time)
                    # Reset counters after waiting
                    self.call_count[key] = 0
                    self.last_call_time[f"{key}_reset"] = time.time()
            
            # Smart delay strategy for parallel execution:
            # - Allow quick bursts for parallel agents (up to 6 simultaneous)
            # - Add minimal delay only if we're getting close to limits
            current_count = self.call_count.get(key, 0)
            
            if current_count > 0:  # Not the first call
                if current_count >= 6:  # After 6 calls, be more conservative
                    min_delay = 6  # 6 seconds for safety
                elif current_count >= 3:  # After 3 calls, small delay
                    min_delay = 2  # 2 seconds 
                else:
                    min_delay = 0.5  # Very small delay for parallel execution
                
                if key in self.last_call_time:
                    time_since_last = now - self.last_call_time[key]
                    if time_since_last < min_delay:
                        sleep_time = min_delay - time_since_last
                        if sleep_time > 1:  # Only show message for longer waits
                            print(f"⏱️  Rate limiting: waiting {sleep_time:.1f}s...")
                        time.sleep(sleep_time)
            
            # Update tracking
            self.call_count[key] = self.call_count.get(key, 0) + 1
            self.last_call_time[key] = time.time()


# Global rate limiter instance
_rate_limiter = RateLimiter()


def call_llm(
    prompt: any,
    pydantic_model: type[BaseModel],
    agent_name: str | None = None,
    state: AgentState | None = None,
    max_retries: int = 3,
    default_factory=None,
) -> BaseModel:
    """
    Makes an LLM call with retry logic, handling both JSON supported and non-JSON supported models.
    Includes rate limiting for Google Gemini free tier.

    Args:
        prompt: The prompt to send to the LLM
        pydantic_model: The Pydantic model class to structure the output
        agent_name: Optional name of the agent for progress updates and model config extraction
        state: Optional state object to extract agent-specific model configuration
        max_retries: Maximum number of retries (default: 3)
        default_factory: Optional factory function to create default response on failure

    Returns:
        An instance of the specified Pydantic model
    """
    
    # Extract model configuration if state is provided and agent_name is available
    model_name = None
    model_provider = None
    if state and agent_name:
        model_name, model_provider = get_agent_model_config(state, agent_name)
    
    # Fallback to defaults if still not provided
    if not model_name:
        model_name = "gpt-4.1"
    if not model_provider:
        model_provider = "OPENAI"

    # Apply rate limiting for Google Gemini
    _rate_limiter.wait_if_needed(model_provider, model_name)

    model_info = get_model_info(model_name, model_provider)
    llm = get_model(model_name, model_provider)

    if llm is None:
        print(f"⚠️  Warning: Could not get LLM model for {model_name} from {model_provider}")
        if default_factory:
            return default_factory()
        return create_default_response(pydantic_model)

    # For non-JSON support models, we can use structured output
    if not (model_info and not model_info.has_json_mode()):
        llm = llm.with_structured_output(
            pydantic_model,
            method="json_mode",
        )

    # Call the LLM with retries
    for attempt in range(max_retries):
        try:
            if agent_name:
                progress.update_status(agent_name, None, f"Calling {model_provider} {model_name}...")

            # Call the LLM
            result = llm.invoke(prompt)

            # For non-JSON support models, we need to extract and parse the JSON manually
            if model_info and not model_info.has_json_mode():
                parsed_result = extract_json_from_response(result.content)
                if parsed_result:
                    return pydantic_model(**parsed_result)
            else:
                return result

        except Exception as e:
            error_msg = str(e).lower()
            
            # Handle rate limiting more gracefully
            if "429" in error_msg or "quota" in error_msg or "rate limit" in error_msg:
                if model_provider.upper() == "GOOGLE":
                    wait_time = min(30 + (attempt * 10), 60)  # Progressive backoff, max 60s
                    print(f"🚫 Google Gemini rate limit hit. Waiting {wait_time}s before retry {attempt + 1}/{max_retries}...")
                    time.sleep(wait_time)
                    continue
            
            if agent_name:
                progress.update_status(agent_name, None, f"Error - retry {attempt + 1}/{max_retries}")

            if attempt == max_retries - 1:
                print(f"❌ Error in LLM call after {max_retries} attempts: {e}")
                # Use default_factory if provided, otherwise create a basic default
                if default_factory:
                    return default_factory()
                return create_default_response(pydantic_model)
            
            # Short delay before retry
            time.sleep(2)

    # This should never be reached due to the retry logic above
    return create_default_response(pydantic_model)


def create_default_response(model_class: type[BaseModel]) -> BaseModel:
    """Creates a safe default response based on the model's fields."""
    default_values = {}
    for field_name, field in model_class.model_fields.items():
        if field.annotation == str:
            default_values[field_name] = "Error in analysis, using default"
        elif field.annotation == float:
            default_values[field_name] = 0.0
        elif field.annotation == int:
            default_values[field_name] = 0
        elif hasattr(field.annotation, "__origin__") and field.annotation.__origin__ == dict:
            default_values[field_name] = {}
        else:
            # For other types (like Literal), try to use the first allowed value
            if hasattr(field.annotation, "__args__"):
                default_values[field_name] = field.annotation.__args__[0]
            else:
                default_values[field_name] = None

    return model_class(**default_values)


def extract_json_from_response(content: str) -> dict | None:
    """Extracts JSON from markdown-formatted response."""
    try:
        json_start = content.find("```json")
        if json_start != -1:
            json_text = content[json_start + 7 :]  # Skip past ```json
            json_end = json_text.find("```")
            if json_end != -1:
                json_text = json_text[:json_end].strip()
                return json.loads(json_text)
    except Exception as e:
        print(f"Error extracting JSON from response: {e}")
    return None


def get_agent_model_config(state, agent_name):
    """
    Get model configuration for a specific agent from the state.
    Falls back to global model configuration if agent-specific config is not available.
    """
    request = state.get("metadata", {}).get("request")

    if agent_name == 'portfolio_manager':
        # Get the model and provider from state metadata
        model_name = state.get("metadata", {}).get("model_name", "gpt-4.1")
        model_provider = state.get("metadata", {}).get("model_provider", "OPENAI")
        return model_name, model_provider
    
    if request and hasattr(request, 'get_agent_model_config'):
        # Get agent-specific model configuration
        model_name, model_provider = request.get_agent_model_config(agent_name)
        return model_name, model_provider.value if hasattr(model_provider, 'value') else str(model_provider)
    
    # Fall back to global configuration
    model_name = state.get("metadata", {}).get("model_name", "gpt-4.1")
    model_provider = state.get("metadata", {}).get("model_provider", "OPENAI")
    
    # Convert enum to string if necessary
    if hasattr(model_provider, 'value'):
        model_provider = model_provider.value
    
    return model_name, model_provider
