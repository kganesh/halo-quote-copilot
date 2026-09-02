from mcp.server.fastmcp import FastMCP

mcp = FastMCP("WeatherStockServer")

@mcp.tool()
def get_weather(city: str) -> dict:
    """Get current weather for a city."""
    mock_data = {
        "london": {"temp_c": 15, "condition": "Cloudy", "humidity": 80},
        "new york": {"temp_c": 22, "condition": "Sunny", "humidity": 55},
        "tokyo": {"temp_c": 28, "condition": "Humid", "humidity": 75},
    }
    data = mock_data.get(city.lower(), {"temp_c": 20, "condition": "Clear", "humidity": 60})
    return {"city": city, **data}

@mcp.tool()
def get_stock_price(symbol: str) -> dict:
    """Get current stock price for a ticker symbol."""
    mock_prices = {
        "AAPL": 189.50,
        "GOOGL": 175.20,
        "MSFT": 415.80,
        "NVDA": 875.00,
    }
    price = mock_prices.get(symbol.upper(), 100.00)
    return {"symbol": symbol.upper(), "price": price, "currency": "USD"}

if __name__ == "__main__":
    mcp.run()