"""
Error diagnostics and troubleshooting utilities for TradingAgents
Provides detailed error information and resolution steps for common issues.
"""

import os
from typing import Dict, List, Optional


class ErrorDiagnostics:
    """Utility class for diagnosing and providing solutions for common errors"""
    
    ERROR_SOLUTIONS = {
        "openai_api_key": {
            "title": "🔑 OpenAI API Key Error",
            "description": "OpenAI API key is missing, invalid, or has insufficient credits",
            "solutions": [
                "1. Check your .env file contains: OPENAI_API_KEY=your_key_here",
                "2. Verify your API key at https://platform.openai.com/api-keys",
                "3. Ensure you have sufficient credits in your OpenAI account",
                "4. Try regenerating your API key if it's old"
            ],
            "links": [
                "OpenAI API Keys: https://platform.openai.com/api-keys",
                "Usage Dashboard: https://platform.openai.com/usage"
            ]
        },
        
        "organization_verification": {
            "title": "🏢 OpenAI Organization Verification Error", 
            "description": "Your OpenAI organization requires verification or has billing issues",
            "solutions": [
                "1. Complete organization verification at https://platform.openai.com/account/organization",
                "2. Add a valid payment method to your account",
                "3. Check if your organization has any usage limits",
                "4. Contact OpenAI support if verification is pending"
            ],
            "links": [
                "Organization Settings: https://platform.openai.com/account/organization",
                "Billing Settings: https://platform.openai.com/account/billing",
                "OpenAI Support: https://help.openai.com/"
            ]
        },
        
        "alpaca_api_key": {
            "title": "📈 Alpaca API Key Error",
            "description": "Alpaca trading API credentials are missing or invalid", 
            "solutions": [
                "1. Get API keys from https://app.alpaca.markets/paper/dashboard/overview",
                "2. Add to .env file: ALPACA_API_KEY=your_key and ALPACA_SECRET_KEY=your_secret",
                "3. Use Alpaca Paper keys only: this build is paper-only and live paths fail closed",
                "4. Check API key permissions include market data access"
            ],
            "links": [
                "Alpaca Dashboard: https://app.alpaca.markets/paper/dashboard/overview",
                "API Documentation: https://alpaca.markets/docs/"
            ]
        },
        
        "rate_limit": {
            "title": "⏱️ API Rate Limit Error",
            "description": "You've exceeded the API request limits",
            "solutions": [
                "1. Wait 60 seconds before retrying",
                "2. Consider upgrading your API plan for higher limits",
                "3. Reduce the frequency of analysis runs",
                "4. Check if multiple instances are running simultaneously"
            ],
            "links": [
                "OpenAI Rate Limits: https://platform.openai.com/docs/guides/rate-limits",
                "Alpaca Rate Limits: https://alpaca.markets/docs/api-references/trading-api/orders/"
            ]
        },
        
        "network_connection": {
            "title": "🌐 Network Connection Error",
            "description": "Unable to connect to external APIs",
            "solutions": [
                "1. Check your internet connection",
                "2. Verify firewall/antivirus isn't blocking the application",
                "3. Try using a different network or VPN",
                "4. Check if the API service is currently available"
            ],
            "links": [
                "OpenAI Status: https://status.openai.com/",
                "Alpaca Status: https://status.alpaca.markets/"
            ]
        },
        
        "insufficient_data": {
            "title": "📊 Insufficient Data Error",
            "description": "Not enough historical data available for analysis",
            "solutions": [
                "1. Try a different ticker symbol",
                "2. Adjust the lookback period (reduce days)",
                "3. Verify the ticker symbol format (BTC/USD for crypto, AAPL for stocks)",
                "4. Check if it's a weekend/holiday when markets are closed"
            ],
            "links": [
                "Market Hours: https://www.alpaca.markets/support/what-are-the-market-hours/",
                "Supported Assets: https://alpaca.markets/docs/api-references/market-data-api/stock-pricing-data/"
            ]
        },
        
        "timeout": {
            "title": "⏰ Operation Timeout Error",
            "description": "A tool or API call took too long to complete",
            "solutions": [
                "1. Check your internet connection speed",
                "2. Try again in a few minutes (may be temporary API slowness)",
                "3. Reduce the amount of data being processed",
                "4. Consider using a different analysis date range"
            ],
            "links": [
                "Network Speed Test: https://fast.com/"
            ]
        }
    }
    
    @classmethod
    def diagnose_error(cls, error_message: str, error_type: str = None) -> Optional[Dict]:
        """
        Diagnose an error and provide solutions
        
        Args:
            error_message: The error message string
            error_type: Optional error type classification
            
        Returns:
            Dict with diagnosis information or None if no match found
        """
        error_lower = error_message.lower()
        
        # Match error patterns to diagnostic categories
        if "api key" in error_lower and "openai" in error_lower:
            return cls.ERROR_SOLUTIONS["openai_api_key"]
        elif "organization" in error_lower and "verification" in error_lower:
            return cls.ERROR_SOLUTIONS["organization_verification"]
        elif "api key" in error_lower and ("alpaca" in error_lower or "trading" in error_lower):
            return cls.ERROR_SOLUTIONS["alpaca_api_key"]
        elif "rate limit" in error_lower or "rate_limit" in error_lower:
            return cls.ERROR_SOLUTIONS["rate_limit"]
        elif any(term in error_lower for term in ["connection", "network", "timeout", "unreachable"]):
            if "timeout" in error_lower or error_type == "TimeoutError":
                return cls.ERROR_SOLUTIONS["timeout"]
            else:
                return cls.ERROR_SOLUTIONS["network_connection"]
        elif "insufficient data" in error_lower or "no data" in error_lower:
            return cls.ERROR_SOLUTIONS["insufficient_data"]
        elif error_type == "TimeoutError":
            return cls.ERROR_SOLUTIONS["timeout"]
        
        return None
    
    @classmethod
    def generate_error_report(cls, error_message: str, error_type: str = None, 
                             tool_name: str = None, context: Dict = None) -> str:
        """
        Generate a comprehensive error report with solutions
        
        Args:
            error_message: The error message
            error_type: Error type classification
            tool_name: Name of the tool that failed
            context: Additional context information
            
        Returns:
            Formatted error report string
        """
        diagnosis = cls.diagnose_error(error_message, error_type)
        
        report = []
        report.append("=" * 60)
        report.append("🚨 TRADING AGENTS ERROR REPORT")
        report.append("=" * 60)
        
        if tool_name:
            report.append(f"🔧 Failed Tool: {tool_name}")
        if error_type:
            report.append(f"⚠️ Error Type: {error_type}")
        
        report.append(f"📝 Error Message: {error_message}")
        report.append("")
        
        if diagnosis:
            report.append(diagnosis["title"])
            report.append("-" * len(diagnosis["title"]))
            report.append(diagnosis["description"])
            report.append("")
            
            report.append("💡 RECOMMENDED SOLUTIONS:")
            for solution in diagnosis["solutions"]:
                report.append(f"   {solution}")
            report.append("")
            
            if diagnosis.get("links"):
                report.append("🔗 HELPFUL LINKS:")
                for link in diagnosis["links"]:
                    report.append(f"   {link}")
                report.append("")
        else:
            report.append("💡 GENERAL TROUBLESHOOTING:")
            report.append("   1. Check your .env file for correct API keys")
            report.append("   2. Verify your internet connection")
            report.append("   3. Ensure APIs have sufficient credits/limits")
            report.append("   4. Try running the analysis again in a few minutes")
            report.append("")
        
        if context:
            report.append("📊 ERROR CONTEXT:")
            for key, value in context.items():
                report.append(f"   {key}: {value}")
            report.append("")
        
        report.append("🆘 If problems persist:")
        report.append("   • Check the GitHub Issues: https://github.com/your-repo/issues")
        report.append("   • Review the logs above for more details")
        report.append("   • Ensure all dependencies are up to date")
        report.append("")
        report.append("=" * 60)
        
        return "\n".join(report)
    
    @classmethod
    def check_configuration(cls) -> List[Dict]:
        """
        Check system configuration and return any issues found
        
        Returns:
            List of configuration issues
        """
        issues = []
        
        # Check API keys
        if not os.getenv("OPENAI_API_KEY"):
            issues.append({
                "type": "missing_config",
                "severity": "high", 
                "message": "OPENAI_API_KEY not found in environment variables",
                "solution": "Add OPENAI_API_KEY to your .env file"
            })
            
        if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
            issues.append({
                "type": "missing_config",
                "severity": "high",
                "message": "Alpaca API credentials not found",
                "solution": "Add ALPACA_API_KEY and ALPACA_SECRET_KEY to your .env file"
            })
        
        return issues


def print_error_diagnosis(error_message: str, error_type: str = None, 
                         tool_name: str = None, context: Dict = None):
    """
    Print a detailed error diagnosis to console
    
    Args:
        error_message: The error message
        error_type: Error type classification  
        tool_name: Name of the tool that failed
        context: Additional context information
    """
    report = ErrorDiagnostics.generate_error_report(
        error_message, error_type, tool_name, context
    )
    print(report)


# Convenience function for quick error checking
def quick_diagnose(error_message: str) -> Optional[str]:
    """
    Quick diagnosis returning just the solution steps
    
    Args:
        error_message: The error message to diagnose
        
    Returns:
        Solution text or None if no diagnosis available
    """
    diagnosis = ErrorDiagnostics.diagnose_error(error_message)
    if diagnosis:
        solutions = "\n".join(diagnosis["solutions"])
        return f"{diagnosis['title']}\n{solutions}"
    return None
