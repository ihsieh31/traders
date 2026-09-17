"""
Ticker symbol format utilities for standardizing crypto and stock ticker formats
"""

import re
from typing import Dict, Tuple


class TickerUtils:
    """Utility class for handling and standardizing ticker symbol formats"""
    
    # Common crypto symbols and their standardized forms
    CRYPTO_SYMBOLS = {
        'BTC', 'ETH', 'ADA', 'SOL', 'DOGE', 'MATIC', 'AVAX', 'DOT', 'LINK', 
        'UNI', 'LTC', 'BCH', 'XRP', 'ATOM', 'ALGO', 'AAVE', 'COMP', 'MKR',
        'SNX', 'YFI', 'SUSHI', 'CRV', '1INCH', 'ENJ', 'BAT', 'ZRX'
    }
    
    @staticmethod
    def _split_crypto_pair(ticker: str) -> Tuple[str, str]:
        """Split a crypto ticker into (base, quote); quote is '' for bare symbols."""
        ticker = ticker.upper()
        if "/" in ticker:
            base, _, quote = ticker.partition("/")
            return base, quote or "USD"
        if "-" in ticker:
            base, _, quote = ticker.partition("-")
            return base, quote or "USD"
        for suffix, quote in (("USDT", "USDT"), ("USDC", "USDC"), ("USD", "USD")):
            if ticker.endswith(suffix) and len(ticker) > len(suffix):
                return ticker[: -len(suffix)], quote
        return ticker, "USD"

    @staticmethod
    def standardize_ticker(ticker: str) -> Dict[str, str]:
        """
        Standardize a ticker symbol and return multiple formats.

        Args:
            ticker: Raw ticker symbol (e.g., "BTC/USD", "BTC-USD", "BTCUSD", "AAPL")

        Returns:
            Dict with standardized formats:
            - 'original': Original input ticker
            - 'base_symbol': Base symbol (BTC from BTC/USD, AAPL from AAPL)
            - 'is_crypto': Boolean indicating if this is a cryptocurrency
            - 'alpaca_format': Format for Alpaca API (BTC/USD for crypto, AAPL for stocks)
            - 'openai_format': Format for OpenAI news APIs (BTCUSD for crypto, AAPL for stocks)
            - 'display_format': Human-readable format (BTC/USD, AAPL)
            - 'clean_symbol': Clean base symbol for APIs that need just the symbol
        """

        if not ticker:
            raise ValueError("Ticker cannot be empty")

        ticker = ticker.strip().upper()

        # Detect if this is a crypto pair
        is_crypto = TickerUtils._is_crypto_ticker(ticker)

        if is_crypto:
            base_symbol, quote = TickerUtils._split_crypto_pair(ticker)
            # D10: keep the actual quote currency — rewriting BTC/USDC to
            # BTC/USD misreports the traded pair. Only USD-quoted pairs are
            # supported end-to-end; other quotes pass through unchanged.
            pair = f"{base_symbol}/{quote}"
            return {
                'original': ticker,
                'base_symbol': base_symbol,
                'is_crypto': True,
                'alpaca_format': pair,                    # Alpaca uses BTC/USD format
                'openai_format': f"{base_symbol}{quote}", # Some APIs use BTCUSD format
                'display_format': pair,                   # Human readable
                'clean_symbol': base_symbol,              # Just BTC
                'yahoo_format': f"{base_symbol}-{quote}", # Yahoo Finance format
                'coindesk_format': base_symbol            # CoinDesk format
            }
        else:
            # Stock ticker — D10: preserve share-class separators (BRK.B is
            # a different security than the stripped BRKB); only whitespace
            # is removed.
            clean_ticker = re.sub(r'\s+', '', ticker)
            return {
                'original': ticker,
                'base_symbol': clean_ticker,
                'is_crypto': False,
                'alpaca_format': clean_ticker,      # AAPL / BRK.B
                'openai_format': clean_ticker,      # AAPL / BRK.B
                'display_format': clean_ticker,     # AAPL / BRK.B
                'clean_symbol': clean_ticker,       # AAPL / BRK.B
                'yahoo_format': clean_ticker.replace('.', '-'),  # BRK.B -> BRK-B
                'coindesk_format': clean_ticker     # AAPL / BRK.B
            }
    
    @staticmethod
    def _is_crypto_ticker(ticker: str) -> bool:
        """Detect if a ticker represents a cryptocurrency"""
        ticker = ticker.upper()
        
        # Check for crypto indicators in the ticker
        crypto_indicators = [
            '/', '-', 'USD', 'USDT', 'USDC', 'BTC', 'ETH'
        ]
        
        # If contains common crypto pair indicators
        if any(indicator in ticker for indicator in crypto_indicators):
            # Extract potential base symbol
            base = TickerUtils._extract_crypto_base(ticker)
            if base in TickerUtils.CRYPTO_SYMBOLS:
                return True
        
        # Check if the ticker itself is a known crypto symbol
        if ticker in TickerUtils.CRYPTO_SYMBOLS:
            return True
            
        return False
    
    @staticmethod
    def _extract_crypto_base(ticker: str) -> str:
        """Extract the base cryptocurrency symbol from a ticker pair"""
        ticker = ticker.upper()
        
        # Handle different formats
        if "/" in ticker:
            # BTC/USD -> BTC
            return ticker.split('/')[0]
        elif "-" in ticker:
            # BTC-USD -> BTC
            return ticker.split('-')[0]
        elif ticker.endswith('USDT'):
            # BTCUSDT -> BTC
            return ticker[:-4]
        elif ticker.endswith('USDC'):
            # BTCUSDC -> BTC
            return ticker[:-4]
        elif ticker.endswith('USD'):
            # BTCUSD -> BTC
            return ticker[:-3]
        else:
            # Assume it's already the base symbol
            return ticker
    
    @staticmethod
    def convert_for_api(ticker: str, api_name: str) -> str:
        """
        Convert a ticker to the format required by a specific API
        
        Args:
            ticker: Original ticker symbol
            api_name: API name ('alpaca', 'openai', 'yahoo', 'coindesk', etc.)
            
        Returns:
            Ticker in the format required by the specified API
        """
        standardized = TickerUtils.standardize_ticker(ticker)
        
        api_formats = {
            'alpaca': standardized['alpaca_format'],
            'openai': standardized['openai_format'], 
            'yahoo': standardized['yahoo_format'],
            'coindesk': standardized['coindesk_format'],
            'display': standardized['display_format'],
            'clean': standardized['clean_symbol']
        }
        
        return api_formats.get(api_name.lower(), standardized['original'])
    
    @staticmethod
    def get_symbol_info(ticker: str) -> Dict[str, any]:
        """
        Get comprehensive information about a ticker symbol
        
        Returns:
            Dict with ticker information including all formats and metadata
        """
        standardized = TickerUtils.standardize_ticker(ticker)
        
        # Add additional metadata
        info = standardized.copy()
        info.update({
            'symbol_type': 'cryptocurrency' if standardized['is_crypto'] else 'stock',
            'description': f"{'Cryptocurrency pair' if standardized['is_crypto'] else 'Stock symbol'}: {standardized['display_format']}"
        })
        
        return info


def normalize_ticker_for_logs(ticker: str) -> str:
    """
    Normalize ticker for consistent logging across the application
    
    Args:
        ticker: Raw ticker symbol
        
    Returns:
        Normalized ticker for logging purposes
    """
    try:
        info = TickerUtils.standardize_ticker(ticker)
        return info['display_format']
    except:
        return ticker  # Fallback to original if standardization fails


# Convenience functions for backward compatibility
def is_crypto_ticker(ticker: str) -> bool:
    """Check if ticker is a cryptocurrency"""
    return TickerUtils._is_crypto_ticker(ticker)

def get_base_crypto_symbol(ticker: str) -> str:
    """Extract base crypto symbol (BTC from BTC/USD)"""
    return TickerUtils._extract_crypto_base(ticker)

def format_for_alpaca(ticker: str) -> str:
    """Format ticker for Alpaca API"""
    return TickerUtils.convert_for_api(ticker, 'alpaca')

def format_for_openai_news(ticker: str) -> str:
    """Format ticker for OpenAI news APIs"""
    return TickerUtils.convert_for_api(ticker, 'openai')
