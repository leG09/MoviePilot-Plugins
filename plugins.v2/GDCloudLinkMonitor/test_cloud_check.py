#!/usr/bin/env python3
"""
网盘限制检测功能测试脚本
"""

import subprocess
import sys
from pathlib import Path

def test_clouddrive_cli(cli_path: str, test_file: str, target_path: str) -> bool:
    """
    测试 clouddrive-cli 工具是否正常工作
    
    Args:
        cli_path: clouddrive-cli 工具路径
        test_file: 测试文件路径
        target_path: 目标路径
        
    Returns:
        bool: 测试是否成功
    """
    try:
        # 检查文件是否存在
        if not Path(cli_path).exists():
            print(f"错误: clouddrive-cli 工具不存在: {cli_path}")
            return False
            
        if not Path(test_file).exists():
            print(f"错误: 测试文件不存在: {test_file}")
            return False
        
        # 构建命令
        cmd = [
            cli_path,
            "-command=upload",
            f"-file={test_file}",
            f"-path={target_path}/"
        ]
        
        print(f"执行命令: {' '.join(cmd)}")
        
        # 执行命令
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        print(f"返回码: {result.returncode}")
        print(f"标准输出: {result.stdout}")
        print(f"错误输出: {result.stderr}")
        
        # 判断结果
        if result.returncode == 0 and "文件上传完成" in result.stdout:
            print("✅ 网盘状态检测成功 - 网盘正常")
            return True
        else:
            print("❌ 网盘状态检测失败 - 网盘被限制或出现错误")
            return False
            
    except subprocess.TimeoutExpired:
        print("❌ 命令执行超时")
        return False
    except Exception as e:
        print(f"❌ 执行异常: {str(e)}")
        return False

def main():
    """主函数"""
    print("=== 网盘限制检测功能测试 ===\n")
    
    # 测试参数
    cli_path = input("请输入 clouddrive-cli 工具路径: ").strip()
    test_file = input("请输入测试文件路径: ").strip()
    target_path = input("请输入目标路径 (如 /gd10/): ").strip()
    
    if not cli_path or not test_file or not target_path:
        print("错误: 所有参数都必须提供")
        sys.exit(1)
    
    print(f"\n开始测试...")
    print(f"CLI工具: {cli_path}")
    print(f"测试文件: {test_file}")
    print(f"目标路径: {target_path}")
    print("-" * 50)
    
    # 执行测试
    success = test_clouddrive_cli(cli_path, test_file, target_path)
    
    print("-" * 50)
    if success:
        print("🎉 测试成功！网盘限制检测功能可以正常工作")
    else:
        print("💥 测试失败！请检查配置和网络连接")
    
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
