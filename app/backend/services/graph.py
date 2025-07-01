import asyncio
import json
from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

from src.agents.portfolio_manager import portfolio_management_agent
from src.agents.risk_manager import risk_management_agent
from src.main import start
from src.utils.analysts import ANALYST_CONFIG
from src.graph.state import AgentState


# Helper function to create the agent graph
def create_graph(selected_agents: list[str]) -> StateGraph:
    """Create the workflow with selected agents."""
    graph = StateGraph(AgentState)
    graph.add_node("start_node", start)

    # Filter out any agents that are not in analyst.py
    selected_agents = [agent for agent in selected_agents if agent in ANALYST_CONFIG]

    # Get analyst nodes from the configuration
    analyst_nodes = {key: (f"{key}_agent", config["agent_func"]) for key, config in ANALYST_CONFIG.items()}

    # Add selected analyst nodes
    for agent_name in selected_agents:
        node_name, node_func = analyst_nodes[agent_name]
        graph.add_node(node_name, node_func)

    # Smart execution strategy based on number of agents
    # For Google Gemini free tier (10 requests/minute):
    # - Up to 6 agents: Run in parallel (6 agents + risk + portfolio = 8 total calls)
    # - 7+ agents: Run sequentially to avoid rate limits
    
    if len(selected_agents) <= 6:
        # PARALLEL execution for small numbers - much faster!
        print(f"🚀 Running {len(selected_agents)} agents in PARALLEL for faster execution")
        for agent_name in selected_agents:
            node_name = analyst_nodes[agent_name][0]
            graph.add_edge("start_node", node_name)
            graph.add_edge(node_name, "risk_management_agent")
    else:
        # Sequential execution only for large numbers (7+ agents)
        print(f"⏳ Running {len(selected_agents)} agents SEQUENTIALLY to respect rate limits")
        graph.add_edge("start_node", f"{selected_agents[0]}_agent")
        for i in range(len(selected_agents) - 1):
            current_agent = f"{selected_agents[i]}_agent"
            next_agent = f"{selected_agents[i + 1]}_agent"
            graph.add_edge(current_agent, next_agent)
        
        # Connect last agent to risk management
        last_agent = f"{selected_agents[-1]}_agent"
        graph.add_edge(last_agent, "risk_management_agent")

    # Always add risk and portfolio management (for now)
    graph.add_node("risk_management_agent", risk_management_agent)
    graph.add_node("portfolio_manager", portfolio_management_agent)

    # If no selected agents, connect start directly to risk management
    if not selected_agents:
        graph.add_edge("start_node", "risk_management_agent")

    # Connect the risk management agent to the portfolio management agent
    graph.add_edge("risk_management_agent", "portfolio_manager")

    # Connect the portfolio management agent to the end node
    graph.add_edge("portfolio_manager", END)

    # Set the entry point to the start node
    graph.set_entry_point("start_node")
    return graph


async def run_graph_async(graph, portfolio, tickers, start_date, end_date, model_name, model_provider, request=None):
    """Async wrapper for run_graph to work with asyncio."""
    # Use run_in_executor to run the synchronous function in a separate thread
    # so it doesn't block the event loop
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: run_graph(graph, portfolio, tickers, start_date, end_date, model_name, model_provider, request))  # Use default executor
    return result


def run_graph(
    graph: StateGraph,
    portfolio: dict,
    tickers: list[str],
    start_date: str,
    end_date: str,
    model_name: str,
    model_provider: str,
    request=None,
) -> dict:
    """
    Run the graph with the given portfolio, tickers,
    start date, end date, show reasoning, model name,
    and model provider.
    """
    return graph.invoke(
        {
            "messages": [
                HumanMessage(
                    content="Make trading decisions based on the provided data.",
                )
            ],
            "data": {
                "tickers": tickers,
                "portfolio": portfolio,
                "start_date": start_date,
                "end_date": end_date,
                "analyst_signals": {},
            },
            "metadata": {
                "show_reasoning": False,
                "model_name": model_name,
                "model_provider": model_provider,
                "request": request,  # Pass the request for agent-specific model access
            },
        },
    )


def parse_hedge_fund_response(response):
    """Parses a JSON string and returns a dictionary."""
    try:
        return json.loads(response)
    except json.JSONDecodeError as e:
        print(f"JSON decoding error: {e}\nResponse: {repr(response)}")
        return None
    except TypeError as e:
        print(f"Invalid response type (expected string, got {type(response).__name__}): {e}")
        return None
    except Exception as e:
        print(f"Unexpected error while parsing response: {e}\nResponse: {repr(response)}")
        return None
