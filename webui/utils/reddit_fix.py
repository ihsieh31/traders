"""
Reddit API and data directory fix utility
"""

import os
import json
from datetime import datetime


def check_reddit_data_directory():
    """Check if Reddit data directory exists and is properly set up"""
    try:
        from tradingagents.dataflows.config import DATA_DIR
        reddit_data_path = os.path.join(DATA_DIR, "reddit_data")
        
        print(f"📁 Checking Reddit data directory: {reddit_data_path}")
        
        if not os.path.exists(reddit_data_path):
            print("❌ Reddit data directory not found")
            return False
            
        # Check for required subdirectories
        required_dirs = ["company_news", "global_news"]
        missing_dirs = []
        
        for dir_name in required_dirs:
            dir_path = os.path.join(reddit_data_path, dir_name)
            if not os.path.exists(dir_path):
                missing_dirs.append(dir_name)
                
        if missing_dirs:
            print(f"❌ Missing Reddit subdirectories: {missing_dirs}")
            return False
            
        # Check if directories have data files
        for dir_name in required_dirs:
            dir_path = os.path.join(reddit_data_path, dir_name)
            files = [f for f in os.listdir(dir_path) if f.endswith('.jsonl')]
            if not files:
                print(f"⚠️ No .jsonl files found in {dir_name}")
            else:
                print(f"✅ Found {len(files)} data files in {dir_name}")
                
        return True
        
    except Exception as e:
        print(f"❌ Error checking Reddit data: {e}")
        return False


def create_mock_reddit_data():
    """Create mock Reddit data to prevent hanging when real data is missing"""
    try:
        from tradingagents.dataflows.config import DATA_DIR
        reddit_data_path = os.path.join(DATA_DIR, "reddit_data")
        
        # Create directories
        os.makedirs(os.path.join(reddit_data_path, "company_news"), exist_ok=True)
        os.makedirs(os.path.join(reddit_data_path, "global_news"), exist_ok=True)
        
        # Create mock data file for company news
        mock_data = {
            "created_utc": int(datetime.now().timestamp()),
            "title": "Mock Reddit Post - No Real Data Available",
            "selftext": "This is mock data generated because no real Reddit data was found. Please ensure Reddit data is properly configured.",
            "url": "https://reddit.com/",
            "ups": 1,
            "num_comments": 0
        }
        
        company_file = os.path.join(reddit_data_path, "company_news", "stocks.jsonl")
        global_file = os.path.join(reddit_data_path, "global_news", "worldnews.jsonl")

        # U19: never overwrite an existing data file — the mock must only
        # fill a missing file, otherwise a manual diagnose run could destroy
        # real collected Reddit data with placeholder rows.
        targets = []
        if not os.path.exists(company_file):
            targets.append(company_file)
        if not os.path.exists(global_file):
            targets.append(global_file)
        if not targets:
            print("ℹ️ Existing Reddit data files left untouched (no mock written)")
            return True
        for target in targets:
            with open(target, 'w') as f:
                f.write(json.dumps(mock_data) + '\n')

        print("✅ Created mock Reddit data files")
        return True
        
    except Exception as e:
        print(f"❌ Error creating mock Reddit data: {e}")
        return False


def diagnose_reddit_issues():
    """Diagnose common Reddit-related issues that cause hanging"""
    print("🔍 Diagnosing Reddit API issues...")
    
    # Check 1: Data directory
    if not check_reddit_data_directory():
        print("\n💡 Fixing: Creating mock Reddit data...")
        if create_mock_reddit_data():
            print("✅ Reddit data directory fixed with mock data")
        else:
            print("❌ Failed to create mock data")
            return False
    
    # Check 2: Network connectivity (simple check)
    try:
        import requests
        response = requests.get("https://www.reddit.com", timeout=5)
        if response.status_code == 200:
            print("✅ Reddit.com is accessible")
        else:
            print(f"⚠️ Reddit.com returned status {response.status_code}")
    except Exception as e:
        print(f"⚠️ Reddit.com connectivity issue: {e}")
        print("   This might cause Social Analyst to hang")
    
    print("\n✅ Reddit diagnosis complete")
    return True


if __name__ == "__main__":
    diagnose_reddit_issues() 