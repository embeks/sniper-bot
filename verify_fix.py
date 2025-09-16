#!/usr/bin/env python3
"""
verify_fix.py - Run this to verify all fixes are working
"""

import os
import sys

def check_env_file():
    """Check .env file for common issues"""
    print("=" * 60)
    print("CHECKING .ENV FILE")
    print("=" * 60)
    
    if not os.path.exists('.env'):
        print("❌ .env file not found!")
        return False
    
    with open('.env', 'r') as f:
        lines = f.readlines()
    
    issues = []
    for line in lines:
        if 'PUMPFUN_PROGRAM_ID' in line:
            if '=' in line:
                value = line.split('=', 1)[1]
                # Check for quotes
                if '"' in value or "'" in value:
                    issues.append("PUMPFUN_PROGRAM_ID has quotes - remove them")
                # Check for trailing whitespace
                if value != value.strip():
                    issues.append("PUMPFUN_PROGRAM_ID has trailing whitespace")
                # Check length
                stripped = value.strip()
                if stripped and len(stripped) != 44:
                    issues.append(f"PUMPFUN_PROGRAM_ID wrong length: {len(stripped)} (should be 44)")
                
                print(f"PUMPFUN_PROGRAM_ID value: [{stripped}]")
                print(f"Length: {len(stripped)}")
    
    if issues:
        print("\n❌ Issues found:")
        for issue in issues:
            print(f"  - {issue}")
        return False
    else:
        print("✅ .env file looks good")
        return True

def test_config_loading():
    """Test config.py loads correctly"""
    print("\n" + "=" * 60)
    print("TESTING CONFIG LOADING")
    print("=" * 60)
    
    try:
        import config
        cfg = config.load()
        
        print(f"PUMPFUN_PROGRAM_ID: [{cfg.PUMPFUN_PROGRAM_ID}]")
        print(f"Length: {len(cfg.PUMPFUN_PROGRAM_ID)}")
        
        if len(cfg.PUMPFUN_PROGRAM_ID) != 44:
            print(f"❌ Wrong length: {len(cfg.PUMPFUN_PROGRAM_ID)}")
            return False
        
        # Check if it's valid base58
        valid_chars = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        if not all(c in valid_chars for c in cfg.PUMPFUN_PROGRAM_ID):
            print("❌ Contains invalid characters")
            return False
        
        print("✅ Config loads correctly")
        return True
        
    except Exception as e:
        print(f"❌ Config loading failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_pubkey_parsing():
    """Test Pubkey parsing works"""
    print("\n" + "=" * 60)
    print("TESTING PUBKEY PARSING")
    print("=" * 60)
    
    try:
        from solders.pubkey import Pubkey
        
        # Test with the correct program ID
        test_id = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        parsed = Pubkey.from_string(test_id)
        print(f"✅ Direct parsing works: {parsed}")
        
        # Test with config value
        import config
        cfg = config.load()
        parsed_from_config = Pubkey.from_string(cfg.PUMPFUN_PROGRAM_ID)
        print(f"✅ Config value parsing works: {parsed_from_config}")
        
        return True
        
    except Exception as e:
        print(f"❌ Pubkey parsing failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_pumpfun_buy_import():
    """Test pumpfun_buy.py imports correctly"""
    print("\n" + "=" * 60)
    print("TESTING PUMPFUN_BUY IMPORT")
    print("=" * 60)
    
    try:
        import pumpfun_buy
        print(f"PUMPFUN_PROGRAM_ID: {pumpfun_buy.PUMPFUN_PROGRAM_ID}")
        print("✅ pumpfun_buy imports successfully")
        return True
        
    except Exception as e:
        print(f"❌ pumpfun_buy import failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run all tests"""
    print("\n🔧 RUNNING VERIFICATION TESTS\n")
    
    results = []
    
    # Run tests
    results.append(("ENV File", check_env_file()))
    results.append(("Config Loading", test_config_loading()))
    results.append(("Pubkey Parsing", test_pubkey_parsing()))
    results.append(("PumpFun Buy Import", test_pumpfun_buy_import()))
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    all_passed = True
    for test_name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"{test_name}: {status}")
        if not passed:
            all_passed = False
    
    if all_passed:
        print("\n🎉 ALL TESTS PASSED! Your bot should now execute buys correctly.")
        print("\nNext steps:")
        print("1. Restart your bot")
        print("2. Monitor for new token detections")
        print("3. Watch for successful buy executions")
    else:
        print("\n⚠️ Some tests failed. Fix the issues above and run this script again.")
        print("\nCommon fixes:")
        print("1. Remove quotes from PUMPFUN_PROGRAM_ID in .env")
        print("2. Remove any trailing spaces/newlines from .env values")
        print("3. Ensure PUMPFUN_PROGRAM_ID is exactly: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
        sys.exit(1)

if __name__ == "__main__":
    main()
