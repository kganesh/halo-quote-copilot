import asyncio
import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

client = anthropic.Anthropic()


async def run_agent(user_message: str):
    print(f"\nUser: {user_message}")
    print("-" * 50)

    server_params = StdioServerParameters(
        command="python",
        args=["weather_server.py"],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Fetch tools from MCP server and convert to Anthropic format
            tools_result = await session.list_tools()
            tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema,
                }
                for t in tools_result.tools
            ]
            print(f"MCP tools available: {[t['name'] for t in tools]}\n")

            messages = [{"role": "user", "content": user_message}]

            # Agentic loop — keeps running until Claude stops requesting tools
            while True:
                response = client.messages.create(
                    model="claude-opus-4-8",
                    max_tokens=1024,
                    tools=tools,
                    messages=messages,
                )

                if response.stop_reason == "end_turn":
                    # Claude is done — print final response
                    for block in response.content:
                        if hasattr(block, "text"):
                            print(f"Claude: {block.text}")
                    break

                if response.stop_reason == "tool_use":
                    # Claude wants to call one or more tools
                    messages.append({"role": "assistant", "content": response.content})

                    tool_results = []
                    for block in response.content:
                        if block.type == "tool_use":
                            print(f"  → Calling: {block.name}({block.input})")
                            result = await session.call_tool(block.name, block.input)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result.content[0].text,
                            })

                    # Feed tool results back to Claude
                    messages.append({"role": "user", "content": tool_results})

    print("-" * 50)


if __name__ == "__main__":
    asyncio.run(run_agent("What's the weather like in Tokyo and Mumbai right now?"))
    asyncio.run(run_agent("What are the current prices for AAPL, NVDA, and TSLA?"))
    asyncio.run(run_agent("I'm visiting London next week. What's the weather there? Also check MSFT stock."))