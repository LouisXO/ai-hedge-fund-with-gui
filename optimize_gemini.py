#!/usr/bin/env python3
"""
Google Gemini Free Tier Optimization Utility

This script helps optimize your AI Hedge Fund experience when using 
Google Gemini's free tier (10 requests per minute limit).
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path


class GeminiOptimizer:
    def __init__(self):
        self.cache_dir = Path("cache")
        self.cache_dir.mkdir(exist_ok=True)
        self.usage_file = self.cache_dir / "gemini_usage.json"
        
    def check_rate_limit_status(self):
        """Check current rate limit status"""
        try:
            with open(self.usage_file, 'r') as f:
                usage_data = json.load(f)
            
            last_reset = datetime.fromisoformat(usage_data.get('last_reset', '2000-01-01'))
            call_count = usage_data.get('call_count', 0)
            
            # Reset if more than a minute has passed
            if datetime.now() - last_reset > timedelta(minutes=1):
                call_count = 0
                
            remaining = max(0, 10 - call_count)
            time_until_reset = 60 - (datetime.now() - last_reset).seconds
            
            return {
                'remaining_calls': remaining,
                'time_until_reset': time_until_reset,
                'calls_used': call_count
            }
        except:
            return {
                'remaining_calls': 10,
                'time_until_reset': 60,
                'calls_used': 0
            }
    
    def suggest_agent_combinations(self):
        """Suggest optimal agent combinations for free tier"""
        combinations = {
            "Quick Analysis (3 calls)": [
                "warren_buffett",
                "cathie_wood", 
                "risk_manager"
            ],
            "Value Focus (4 calls)": [
                "warren_buffett",
                "ben_graham",
                "peter_lynch",
                "risk_manager"
            ],
            "Growth Focus (4 calls)": [
                "cathie_wood",
                "peter_lynch",
                "phil_fisher",
                "risk_manager"
            ],
            "Technical Analysis (3 calls)": [
                "technicals",
                "sentiment",
                "risk_manager"
            ],
            "Conservative Analysis (5 calls)": [
                "warren_buffett",
                "ben_graham",
                "charlie_munger",
                "risk_manager",
                "fundamentals"
            ]
        }
        return combinations
    
    def estimate_analysis_time(self, num_agents):
        """Estimate how long analysis will take with rate limiting"""
        # Each agent + risk_manager + portfolio_manager
        total_calls = num_agents + 2
        
        if num_agents <= 6:
            # PARALLEL execution for up to 6 agents - much faster!
            # First 3 agents: ~2 seconds (parallel with small delays)
            # Next 3 agents: ~6 seconds (parallel with medium delays)  
            # Risk manager: ~2 seconds
            # Portfolio manager: ~2 seconds
            if num_agents <= 3:
                return f"~{8 + num_agents * 2} seconds (parallel execution)"
            else:
                return f"~{12 + num_agents * 2} seconds (parallel execution)"
        else:
            # Sequential execution for 7+ agents
            minutes_needed = (total_calls // 10) + (1 if total_calls % 10 > 0 else 0)
            return f"~{minutes_needed} minutes (sequential execution)"
    
    def print_optimization_report(self):
        """Print comprehensive optimization report"""
        print("🤖 Google Gemini Free Tier Optimization Report")
        print("=" * 50)
        
        # Current status
        status = self.check_rate_limit_status()
        print(f"📊 Current Rate Limit Status:")
        print(f"   • Remaining calls: {status['remaining_calls']}/10")
        print(f"   • Calls used: {status['calls_used']}")
        print(f"   • Reset in: {status['time_until_reset']} seconds")
        print()
        
        # Suggested combinations
        print("💡 Suggested Agent Combinations:")
        combinations = self.suggest_agent_combinations()
        for name, agents in combinations.items():
            estimate = self.estimate_analysis_time(len(agents))
            print(f"   📈 {name}")
            print(f"      Agents: {', '.join(agents)}")
            print(f"      Time: {estimate}")
            print()
        
        # Tips
        print("🎯 Optimization Tips:")
        tips = [
            "✨ Up to 6 agents now run in PARALLEL - much faster than before!", 
            "🚀 Use 3 agents for ~15 second analysis (warren_buffett + cathie_wood + risk_manager)",
            "⚡ Use 4-6 agents for ~20-25 second analysis with great coverage",
            "🐌 Only 7+ agents run sequentially to respect rate limits",
            "💡 Parallel execution is 3-4x faster than sequential",
            "🔄 The system automatically chooses the best execution strategy"
        ]
        
        for i, tip in enumerate(tips, 1):
            print(f"   {i}. {tip}")
        
        print()
        print("⚡ Performance Comparison:")
        print("   • Google Gemini Free: 10 calls/min, remote processing")
        print("   • Google Gemini Pro: 1000 calls/min, remote processing") 
        print("   • Ollama Local: Unlimited calls, local processing")
        print()


def main():
    """Main function to run optimization utility"""
    optimizer = GeminiOptimizer()
    
    print("🚀 AI Hedge Fund - Google Gemini Optimizer")
    print()
    
    while True:
        print("Choose an option:")
        print("1. 📊 Show rate limit status")
        print("2. 💡 Show agent combinations")
        print("3. ⏱️  Estimate analysis time")
        print("4. 📋 Full optimization report")
        print("5. 🚪 Exit")
        
        choice = input("\nEnter choice (1-5): ").strip()
        
        if choice == '1':
            status = optimizer.check_rate_limit_status()
            print(f"\n📊 Rate Limit Status:")
            print(f"Remaining: {status['remaining_calls']}/10 calls")
            print(f"Used: {status['calls_used']} calls")
            print(f"Reset in: {status['time_until_reset']} seconds")
            
        elif choice == '2':
            combinations = optimizer.suggest_agent_combinations()
            print("\n💡 Recommended Agent Combinations:")
            for name, agents in combinations.items():
                print(f"\n📈 {name}:")
                print(f"   {', '.join(agents)}")
                
        elif choice == '3':
            try:
                num = int(input("Enter number of agents: "))
                estimate = optimizer.estimate_analysis_time(num)
                print(f"\n⏱️  Estimated time: {estimate}")
            except ValueError:
                print("Please enter a valid number")
                
        elif choice == '4':
            optimizer.print_optimization_report()
            
        elif choice == '5':
            print("👋 Happy trading!")
            break
            
        else:
            print("Invalid choice. Please try again.")
        
        print("\n" + "-" * 40 + "\n")


if __name__ == "__main__":
    main() 