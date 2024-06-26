import os
import fitz  # PyMuPDF
import json
from datetime import datetime
from urllib.parse import urlparse

# Load configuration from config.json
with open('config.json', 'r') as config_file:
    config = json.load(config_file)

rootURL = config['rootURL']
pdfDir = config['pdfDir']

def ensure_directory_exists(directory):
    if not os.path.exists(directory):
        os.makedirs(directory)

def clean_filename(filename):
    name_without_extension = os.path.splitext(filename)[0]
    parts = name_without_extension.split('-')
    cleaned_name = '-'.join(parts[1:])
    return cleaned_name

def get_article_titles():
    try:
        article_titles_file_path = os.path.join(pdfDir, 'articleTitles.json')
        with open(article_titles_file_path, 'r', encoding='utf-8') as f:
            article_titles = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        article_titles = {}
    return article_titles

def get_number_prefix(file_name):
    return int(file_name.split('-')[0])

def get_article_title_by_number_prefix(number_prefix, article_titles):
    return article_titles.get(str(number_prefix), '')

def merge_pdfs_in_directory(directory_path, output_file_name):
    files = [f for f in os.listdir(directory_path) if f.endswith('.pdf')]
    files.sort(key=lambda x: int(x.split('-')[0]))

    if not files:
        return

    article_titles = get_article_titles()
    merged_pdf = fitz.open()
    toc = []
    for file in files:
        file_path = os.path.join(directory_path, file)
        pdf = fitz.open(file_path)
        merged_pdf.insert_pdf(pdf)
        bookmark_title = clean_filename(file)
        number_prefix = get_number_prefix(file)
        article_title = get_article_title_by_number_prefix(number_prefix, article_titles)
        if article_title:
            bookmark_title = article_title
        start_page = len(merged_pdf) - pdf.page_count
        toc.append([1, bookmark_title, start_page + 1, {"kind": 1, "page": start_page + 1}])
        pdf.close()

    merged_pdf.set_toc(toc)
    merged_pdf.save(output_file_name)
    merged_pdf.close()
    print(f'Merged PDF saved as: {output_file_name}')

def merge_pdfs_for_root_and_subdirectories():
    url = urlparse(rootURL)
    domain = url.hostname.replace('.', '_')
    current_date = datetime.now().strftime('%Y%m%d')
    final_pdf_directory = os.path.join(pdfDir, "finalPdf")
    ensure_directory_exists(final_pdf_directory)

    root_output_file_name = os.path.join(final_pdf_directory, f"{domain}_{current_date}.pdf")
    merge_pdfs_in_directory(pdfDir, root_output_file_name)

    directories = [d for d in os.listdir(pdfDir) if os.path.isdir(os.path.join(pdfDir, d)) and d != "finalPdf"]

    for directory in directories:
        directory_path = os.path.join(pdfDir, directory)
        output_file_name = os.path.join(final_pdf_directory, f"{directory}_{current_date}.pdf")
        merge_pdfs_in_directory(directory_path, output_file_name)

if __name__ == '__main__':
    merge_pdfs_for_root_and_subdirectories()