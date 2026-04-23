import logging
import os

import trafilatura
from ddgs import DDGS
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def web_search(query: str) -> str:
    """Search the web using DuckDuckGo. Returns a list of results with title, URL, and snippet."""
    max_results = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
    logger.info("[web_search] query=%r max_results=%d", query, max_results)
    try:
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return "No results found."
        lines = []
        for i, result in enumerate(results, 1):
            lines.append(f"{i}. {result.get('title', 'No title')}")
            lines.append(f"   URL: {result.get('href', '')}")
            lines.append(f"   Snippet: {result.get('body', '')}")
            lines.append("")
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("[web_search] failed: %s", exc)
        return f"Search error: {exc}"


@tool
def read_url(url: str) -> str:
    """Fetch and extract the main text content from a URL. Returns up to READ_URL_MAX_CHARS characters."""
    max_chars = int(os.getenv("READ_URL_MAX_CHARS", "5000"))
    logger.info("[read_url] url=%s max_chars=%d", url, max_chars)
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return f"Error: Could not fetch URL: {url}"
        text = trafilatura.extract(downloaded)
        if not text:
            return f"Error: Could not extract text from: {url}"
        return text[:max_chars]
    except Exception as exc:
        logger.warning("[read_url] failed: %s", exc)
        return f"Error reading URL {url}: {exc}"
