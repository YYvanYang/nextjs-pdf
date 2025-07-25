#!/usr/bin/env python3
"""
优化的PDF合并服务类 - 修复文件排序支持数字索引
"""

import os
import sys
import json
import logging
import fitz  # PyMuPDF
from datetime import datetime
from urllib.parse import urlparse
from typing import Dict, List, Optional, Callable, Any
import gc
import psutil
import time
import traceback

class PDFMergerError(Exception):
    """PDF合并相关异常"""
    pass

class ConfigurationError(PDFMergerError):
    """配置错误异常"""
    pass

class FileProcessingError(PDFMergerError):
    """文件处理异常"""
    pass

class PDFMerger:
    """
    企业级PDF合并服务类 - 智能排序版本

    特性：
    - 智能文件排序（支持数字索引和哈希前缀）
    - 流式处理，避免内存溢出
    - 完整的错误处理和恢复机制
    - 进度跟踪和性能监控
    """

    def __init__(self, config_path: str = 'config.json', logger: Optional[logging.Logger] = None):
        """
        初始化PDF合并器

        Args:
            config_path: 配置文件路径
            logger: 可选的日志记录器
        """
        self.config_path = config_path
        self.logger = logger or self._setup_logger()

        # 加载配置
        self.config = self._load_config(config_path)

        # 设置路径
        self.pdf_dir = self.config['pdfDir']
        self.metadata_dir = os.path.join(
            self.pdf_dir,
            self.config.get('metadata', {}).get('directory', 'metadata')
        )
        self.final_pdf_dir = os.path.join(
            self.pdf_dir,
            self.config.get('output', {}).get('finalPdfDirectory', 'finalPdf')
        )

        # 性能监控
        self.stats = {
            'files_processed': 0,
            'total_pages': 0,
            'start_time': None,
            'memory_peak': 0,
            'errors': []
        }

        # 加载文章标题
        self.article_titles = self._load_article_titles()

    def _setup_logger(self) -> logging.Logger:
        """设置默认日志记录器"""
        logger = logging.getLogger('PDFMerger')
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            logger.setLevel(logging.WARNING)  # Only show warnings and errors
        return logger

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """加载配置文件"""
        try:
            if not os.path.exists(config_path):
                raise ConfigurationError(f"配置文件不存在: {config_path}")

            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

            # 验证必需的配置项
            required_keys = ['rootURL', 'pdfDir']
            for key in required_keys:
                if key not in config:
                    raise ConfigurationError(f"缺少必需的配置项: {key}")

            return config

        except json.JSONDecodeError as e:
            raise ConfigurationError(f"配置文件JSON格式错误: {e}")
        except Exception as e:
            raise ConfigurationError(f"配置加载失败: {e}")

    def _load_article_titles(self) -> Dict[str, str]:
        """加载文章标题映射"""
        article_titles = {}

        try:
            # 尝试从元数据目录加载
            metadata_file = os.path.join(self.metadata_dir, 'articleTitles.json')
            if os.path.exists(metadata_file):
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    article_titles = json.load(f)
                    pass  # Loaded article titles from metadata

            # 回退到PDF目录
            if not article_titles:
                fallback_file = os.path.join(self.pdf_dir, 'articleTitles.json')
                if os.path.exists(fallback_file):
                    with open(fallback_file, 'r', encoding='utf-8') as f:
                        article_titles = json.load(f)
                        pass  # Loaded article titles from PDF directory

        except Exception as e:
            self.logger.warning(f"加载文章标题失败: {e}")

        return article_titles

    def _get_pdf_files(self, directory_path: str, engine_filter: str = None) -> List[str]:
        """
        获取目录中的PDF文件列表（智能排序）

        支持：
        1. 数字前缀文件（000-xxx.pdf, 001-xxx.pdf）- 按数字排序
        2. 哈希前缀文件（676cb9dd-xxx.pdf）- 按文件创建时间排序
        3. 混合情况 - 数字文件优先，然后哈希文件
        4. 引擎过滤：只获取特定引擎生成的PDF文件

        Args:
            directory_path: 目录路径
            engine_filter: 引擎过滤器，可选值：'puppeteer', None(所有文件)
        """
        try:
            if not os.path.exists(directory_path):
                return []

            all_files = os.listdir(directory_path)
            self.logger.debug(f"目录 {directory_path} 中的所有文件: {all_files}")

            files = [
                f for f in all_files
                if f.endswith('.pdf') and os.path.isfile(os.path.join(directory_path, f))
            ]

            # 根据引擎过滤PDF文件
            if engine_filter:
                if engine_filter == 'puppeteer':
                    # 只要包含_puppeteer的文件
                    files = [f for f in files if '_puppeteer.pdf' in f]
                elif engine_filter == 'single':
                    # 只要不包含_puppeteer的文件（单引擎模式的文件）
                    files = [f for f in files if '_puppeteer.pdf' not in f]

            if not files:
                return []

            self.logger.debug(f"过滤后的PDF文件 (engine_filter={engine_filter}): {files}")

            # 🔥 智能排序逻辑：支持数字前缀和哈希前缀
            def get_sort_key(filename: str) -> tuple:
                try:
                    # 对于双引擎文件，需要去掉_puppeteer后缀来获取前缀
                    name_for_sorting = filename
                    if '_puppeteer.pdf' in filename:
                        name_for_sorting = filename.replace('_puppeteer.pdf', '.pdf')

                    parts = name_for_sorting.split('-', 1)  # 只分割第一个连字符
                    if len(parts) == 0:
                        return (999999, 0, filename)

                    prefix = parts[0]

                    # 检查是否为数字前缀（包括补零的情况）
                    if prefix.isdigit():
                        # 数字前缀，按数字大小排序，优先级最高
                        return (0, int(prefix), filename)

                    # 检查是否为补零的数字前缀（如 001, 002）
                    try:
                        # 去掉前导零，但保留至少一个0
                        num = int(prefix.lstrip('0') or '0')
                        if prefix.startswith('0') and len(prefix) > 1:
                            # 这是补零的数字，优先级最高
                            return (0, num, filename)
                    except ValueError:
                        pass

                    # 检查是否为哈希前缀（8位十六进制）
                    if len(prefix) == 8 and all(c in '0123456789abcdef' for c in prefix.lower()):
                        # 哈希前缀，按文件创建时间排序，优先级次高
                        try:
                            file_path = os.path.join(directory_path, filename)
                            mtime = os.path.getmtime(file_path)
                            return (1, mtime, filename)
                        except:
                            return (1, 0, filename)

                    # 其他情况，按文件名字母排序，优先级最低
                    return (2, 0, filename)

                except Exception as e:
                    self.logger.debug(f"排序键生成失败 {filename}: {e}")
                    return (999999, 0, filename)

            # 按排序键排序
            files.sort(key=get_sort_key)

            # 统计不同类型的文件
            numeric_files = []
            hash_files = []
            other_files = []

            for f in files:
                # 对于双引擎文件，需要去掉引擎后缀来获取前缀
                name_for_analysis = f
                if '_puppeteer.pdf' in f:
                    name_for_analysis = f.replace('_puppeteer.pdf', '.pdf')

                prefix = name_for_analysis.split('-')[0] if '-' in name_for_analysis else ''
                if prefix.isdigit() or (prefix.startswith('0') and prefix.isdigit()):
                    numeric_files.append(f)
                elif len(prefix) == 8 and all(c in '0123456789abcdef' for c in prefix.lower()):
                    hash_files.append(f)
                else:
                    other_files.append(f)

            # Only log if there are significant numbers of files
            if len(files) > 10:
                engine_info = f" ({engine_filter} engine)" if engine_filter else ""
                self.logger.info(f"Found {len(files)} PDF files in {directory_path}{engine_info}")

            self.logger.debug(f"排序后文件列表前5个: {files[:5]}")
            return files

        except Exception as e:
            self.logger.error(f"获取PDF文件列表失败: {e}")
            self.logger.error(f"错误详情: {traceback.format_exc()}")
            return []

    def _create_bookmark_title(self, filename: str, article_titles: Dict[str, str]) -> str:
        """
        创建书签标题（改进版）
        
        🔧 修复：正确处理引擎后缀，避免标题中出现"Puppeteer"

        优先级：
        1. 文章标题映射
        2. 清理后的文件名（移除引擎后缀）
        
        支持的文件名格式：
        - 001-page-name.pdf → "Page Name"
        - 001-page-name_puppeteer.pdf → "Page Name" (移除引擎后缀)
        - 001-page-name_puppeteer.pdf → "Page Name" (移除引擎后缀)
        """
        try:
            self.logger.debug(f"为文件创建书签标题: {filename}")

            # 🔥 首先移除引擎后缀（_puppeteer）
            cleaned_filename = filename
            if '_puppeteer.pdf' in filename:
                cleaned_filename = filename.replace('_puppeteer.pdf', '.pdf')
                self.logger.debug(f"移除Puppeteer引擎后缀: {filename} -> {cleaned_filename}")

            # 提取前缀和文件名部分
            parts = cleaned_filename.split('-', 1)  # 只分割第一个连字符
            if len(parts) < 2:
                title = os.path.splitext(cleaned_filename)[0]
                self.logger.debug(f"无前缀文件，使用文件名作为标题: {title}")
                return title

            prefix = parts[0]
            name_part = parts[1]

            # 🔥 尝试从文章标题映射中查找
            # 支持数字前缀（包括补零）和原始前缀
            possible_keys = [prefix]

            # 如果是数字前缀，添加多种可能的键格式
            if prefix.isdigit():
                num = int(prefix)
                possible_keys.extend([
                    str(num),                    # 去掉前导零: "1"
                    str(num).zfill(3),          # 3位补零: "001"
                    str(num).zfill(2)           # 2位补零: "01"
                ])

            # 查找标题映射
            for key in possible_keys:
                if key in article_titles:
                    title = article_titles[key]
                    self.logger.debug(f"找到文章标题映射 {key}: {title}")
                    return title

            # 如果没找到映射，使用清理后的文件名
            cleaned_name = os.path.splitext(name_part)[0]
            # 将连字符和下划线替换为空格，使用标题格式
            title = cleaned_name.replace('-', ' ').replace('_', ' ')
            # 转换为标题格式：每个单词首字母大写
            title = ' '.join(word.capitalize() for word in title.split())

            self.logger.debug(f"使用清理后的文件名作为标题: {title}")
            return title

        except Exception as e:
            self.logger.warning(f"创建书签标题失败 {filename}: {e}")
            return os.path.splitext(filename)[0]

    def _monitor_memory(self) -> None:
        """监控内存使用情况"""
        try:
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            self.stats['memory_peak'] = max(self.stats['memory_peak'], memory_mb)

            # 如果内存使用超过阈值，强制垃圾回收
            if memory_mb > 500:  # 500MB阈值
                gc.collect()
                self.logger.debug(f"内存使用: {memory_mb:.1f}MB, 已执行垃圾回收")
        except Exception:
            pass  # 内存监控失败不应影响主流程

    def merge_pdfs_stream(
        self,
        directory_path: str,
        output_path: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        engine_filter: str = None
    ) -> bool:
        """流式合并PDF文件"""
        try:
            files = self._get_pdf_files(directory_path, engine_filter)
            if not files:
                return False

            merged_pdf = None
            current_file_pdf = None

            try:
                # 确保输出目录存在
                os.makedirs(os.path.dirname(output_path), exist_ok=True)

                merged_pdf = fitz.open()  # 创建空的PDF文档
                toc = []  # 目录结构

                # Starting merge operation (logging reduced for cleaner output)

                for i, filename in enumerate(files):
                    try:
                        self.logger.debug(f"处理文件 {i+1}/{len(files)}: {filename}")
                        file_path = os.path.join(directory_path, filename)

                        # 检查文件是否存在
                        if not os.path.exists(file_path):
                            self.logger.error(f"文件不存在: {file_path}")
                            continue

                        # 打开当前PDF文件
                        current_file_pdf = fitz.open(file_path)
                        page_count = current_file_pdf.page_count

                        if page_count == 0:
                            self.logger.warning(f"跳过空PDF文件: {filename}")
                            current_file_pdf.close()
                            continue

                        # 记录合并前的页数
                        start_page = merged_pdf.page_count

                        # 插入PDF页面
                        merged_pdf.insert_pdf(current_file_pdf)

                        # 创建书签
                        bookmark_title = self._create_bookmark_title(filename, self.article_titles)
                        toc.append([
                            1,  # 级别
                            bookmark_title,  # 标题
                            start_page + 1,  # 页码（从1开始）
                            {"kind": 1, "page": start_page}  # 链接信息
                        ])

                        # 关闭当前文件
                        current_file_pdf.close()
                        current_file_pdf = None

                        # 更新统计
                        self.stats['files_processed'] += 1
                        self.stats['total_pages'] += page_count

                        # 内存监控
                        self._monitor_memory()

                        # 进度回调
                        if progress_callback:
                            progress_callback(i + 1, len(files))

                        self.logger.debug(f"已合并: {filename} ({page_count} 页) -> 书签: {bookmark_title}")

                    except Exception as e:
                        error_msg = f"处理文件失败 {filename}: {e}"
                        self.logger.error(error_msg)
                        self.logger.error(f"错误详情: {traceback.format_exc()}")
                        self.stats['errors'].append(error_msg)

                        if current_file_pdf:
                            current_file_pdf.close()
                            current_file_pdf = None

                        # 继续处理下一个文件
                        continue

                # 设置目录结构
                if toc:
                    merged_pdf.set_toc(toc)

                # 保存合并后的PDF
                merged_pdf.save(output_path)

                return True

            except Exception as e:
                error_msg = f"PDF合并失败: {e}"
                self.logger.error(error_msg)
                self.logger.error(f"错误详情: {traceback.format_exc()}")
                self.stats['errors'].append(error_msg)
                raise FileProcessingError(error_msg)

            finally:
                # 清理资源
                if current_file_pdf:
                    current_file_pdf.close()
                if merged_pdf:
                    merged_pdf.close()

                # 强制垃圾回收
                gc.collect()

        except Exception as e:
            self.logger.error(f"merge_pdfs_stream 执行失败: {e}")
            self.logger.error(f"错误详情: {traceback.format_exc()}")
            return False

    def _detect_dual_engine_mode(self, directory_path: str) -> bool:
        """检测是否为双引擎模式（已弃用，现在只支持Puppeteer）"""
        # 双引擎模式已移除，始终返回False
        return False

    def merge_directory(self, directory_name: Optional[str] = None) -> List[str]:
        """合并指定目录或所有子目录的PDF文件"""
        try:
            if not os.path.exists(self.pdf_dir):
                raise FileProcessingError(f"PDF目录不存在: {self.pdf_dir}")

            # 确保输出目录存在
            os.makedirs(self.final_pdf_dir, exist_ok=True)

            # 获取域名和日期
            url = urlparse(self.config['rootURL'])
            domain = url.hostname.replace('.', '_') if url.hostname else 'unknown'
            current_date = datetime.now().strftime('%Y%m%d')

            merged_files = []

            if directory_name:
                # 合并指定目录
                directory_path = os.path.join(self.pdf_dir, directory_name)
                if os.path.isdir(directory_path):
                    # 单引擎模式：正常合并
                    output_path = os.path.join(
                        self.final_pdf_dir,
                        f"{directory_name}_{current_date}.pdf"
                    )
                    if self.merge_pdfs_stream(directory_path, output_path):
                        merged_files.append(output_path)
                else:
                    self.logger.warning(f"指定目录不存在: {directory_path}")
            else:
                # 首先合并根目录
                # 单引擎模式：正常合并
                root_output_path = os.path.join(
                    self.final_pdf_dir,
                    f"{domain}_{current_date}.pdf"
                )
                if self.merge_pdfs_stream(self.pdf_dir, root_output_path):
                    merged_files.append(root_output_path)

                # 然后合并所有子目录
                try:
                    items = os.listdir(self.pdf_dir)
                    self.logger.debug(f"PDF目录中的所有项目: {items}")

                    for item in items:
                        try:
                            item_path = os.path.join(self.pdf_dir, item)

                            # 跳过非目录和特殊目录
                            if not os.path.isdir(item_path) or item in ['finalPdf', 'metadata', '.temp']:
                                self.logger.debug(f"跳过项目: {item} (非目录或特殊目录)")
                                continue

                            pass  # Processing subdirectory silently
                            
                            # 单引擎模式：正常合并
                            output_path = os.path.join(
                                self.final_pdf_dir,
                                f"{item}_{current_date}.pdf"
                            )
                            if self.merge_pdfs_stream(item_path, output_path):
                                merged_files.append(output_path)

                        except Exception as e:
                            self.logger.error(f"处理子目录 {item} 时出错: {e}")
                            self.logger.error(f"错误详情: {traceback.format_exc()}")
                            continue

                except Exception as e:
                    self.logger.error(f"列出PDF目录内容时出错: {e}")
                    self.logger.error(f"错误详情: {traceback.format_exc()}")

            return merged_files

        except Exception as e:
            error_msg = f"目录合并失败: {e}"
            self.logger.error(error_msg)
            self.logger.error(f"错误详情: {traceback.format_exc()}")
            raise FileProcessingError(error_msg)

    def get_statistics(self) -> Dict[str, Any]:
        """获取合并统计信息"""
        elapsed_time = 0
        if self.stats['start_time']:
            elapsed_time = time.time() - self.stats['start_time']

        return {
            'files_processed': self.stats['files_processed'],
            'total_pages': self.stats['total_pages'],
            'elapsed_time': elapsed_time,
            'memory_peak_mb': self.stats['memory_peak'],
            'errors_count': len(self.stats['errors']),
            'errors': self.stats['errors'][-10:],  # 最近10个错误
            'avg_pages_per_file': (
                self.stats['total_pages'] / self.stats['files_processed']
                if self.stats['files_processed'] > 0 else 0
            ),
            'processing_speed': (
                self.stats['files_processed'] / elapsed_time
                if elapsed_time > 0 else 0
            )
        }

    def run(self) -> Dict[str, Any]:
        """运行PDF合并任务"""
        self.stats['start_time'] = time.time()

        try:
            # 执行合并
            merged_files = self.merge_directory()

            # 获取统计信息
            stats = self.get_statistics()

            result = {
                'success': True,
                'merged_files': merged_files,
                'statistics': stats
            }

            # Task completed successfully (detailed stats printed separately)

            return result

        except Exception as e:
            error_msg = f"PDF合并任务失败: {e}"
            self.logger.error(error_msg)
            self.logger.error(f"错误详情: {traceback.format_exc()}")

            return {
                'success': False,
                'error': error_msg,
                'statistics': self.get_statistics()
            }

def main():
    """主函数，支持命令行执行"""
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='Smart PDF Merger Tool')
    parser.add_argument('--config', default='config.json', help='Configuration file path')
    parser.add_argument('--directory', help='Specify directory name to merge')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose output')

    args = parser.parse_args()

    # 设置日志级别
    if args.verbose:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.WARNING)

    try:
        # 创建PDF合并器
        merger = PDFMerger(config_path=args.config)

        # 执行合并
        if args.directory:
            merged_files = merger.merge_directory(args.directory)
        else:
            result = merger.run()
            merged_files = result.get('merged_files', [])

        # Output results  
        print(f"\n✅ Merge completed! Generated {len(merged_files)} PDF file(s):")
        for file_path in merged_files:
            print(f"  📄 {file_path}")

        # Output statistics
        stats = merger.get_statistics()
        print(f"\n📊 Statistics:")
        print(f"  - Files processed: {stats['files_processed']}")
        print(f"  - Total pages: {stats['total_pages']}")
        print(f"  - Duration: {stats['elapsed_time']:.1f} seconds")
        print(f"  - Memory peak: {stats['memory_peak_mb']:.1f} MB")

        if stats['errors_count'] > 0:
            print(f"  ⚠️  Errors: {stats['errors_count']}")

        return 0

    except Exception as e:
        print(f"❌ Execution failed: {e}", file=sys.stderr)
        print(f"Error details: {traceback.format_exc()}", file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
