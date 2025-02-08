import PyPDF2
from pydantic import BaseModel
import requests
import os
import pandas as pd
import re
from unidecode import unidecode
from scholarly import scholarly
import logging
from typing import List, Tuple, Dict
from tenacity import retry, stop_after_attempt, wait_exponential

from google import genai
#import google.generativeai as genai



# --- Configuration ---
# Read the API key from the file
with open('api_key', 'r') as file:
    key = file.readline().strip()
GOOGLE_API_KEY = key

# Save your Google Gemini API key in a text file named api_key
# Apply for a key at https://ai.google.dev/aistudio with 1500 requests per day for FREE


# --- Step 1: Read PDF and extract bibliography section ---

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from all pages of the PDF."""
    text = ""
    with open(pdf_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text

def extract_bibliography_section(text: str, keywords: List[str] = ["Reference", "Bibliography", "Works Cited"]) -> str:
    """
    Find the last occurrence of any keyword from 'keywords'
    and return the text from that point onward.
    """
    last_index = -1
    for keyword in keywords:
        index = text.lower().rfind(keyword.lower())
        if index > last_index:
            last_index = index
    if last_index == -1:
        raise ValueError("No bibliography section found using keywords: " + ", ".join(keywords))
    return text[last_index:]


# --- Step 2: Split the bibliography text into individual references ---
def split_references(bib_text):

    class ReferenceExtraction(BaseModel):
        title: str
        authors: list[str]
        DOI: str
        type: str
        input_bibliography: str

    client = genai.Client(api_key=GOOGLE_API_KEY)
    response = client.models.generate_content(
        model='gemini-2.0-flash',
        contents='Here is a list of references, extracted from a PDF file - so please change lines and spaces when necessary. Please extract title, authors, DOI and type (journal_article, book, OR website), and give the original input within one line: \n' + bib_text,
        config={
            'response_mime_type': 'application/json',
            'response_schema': list[ReferenceExtraction],
        },
    )

    # print(response.text)  # JSON string.
    references: list[ReferenceExtraction] = response.parsed  # Parsed JSON.
    return references


# --- Step 3: Verify each reference using crossref and compare title ---
def normalize_title(title: str) -> str:
    """Normalizes a title for comparison (case-insensitive, no punctuation, etc.)."""
    title = unidecode(title)  # Remove accents
    title = re.sub(r'[^\w\s]', '', title).lower()  # Remove punctuation
    title = re.sub(r'\band\b|\bthe\b', '', title)  # Remove 'and' and 'the'
    title = re.sub(r'\s+', '', title).strip()  # Remove extra whitespace
    return title


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def search_title_scholarly(title: str) -> bool:
    """Searches for a title using scholarly, with error handling and retries."""
    try:
        search_results = scholarly.search_pubs(title)
        result = next(search_results, None)  # Safely get the first result, or None
        if result and 'bib' in result and 'title' in result['bib']:
            return normalize_title(result['bib']['title']) == normalize_title(title)
        return False  # No result, or missing title
    except Exception as e:
        logging.warning(f"Scholarly search failed for title '{title}': {e}")
        return False


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def search_title_crossref(title: str) -> bool:
    """Searches for a title using the Crossref API, with retries and more robust matching."""
    params = {'query.title': title, 'rows': 5}  # Increased rows
    response = requests.get("https://api.crossref.org/works", params=params)

    if response.status_code == 200:
        items = response.json().get('message', {}).get('items', [])
        normalized_input_title = normalize_title(title)

        for item in items:
            if 'title' in item and item['title']:
                item_title = item['title'][0]
                normalized_item_title = normalize_title(item_title)
                if normalized_item_title == normalized_input_title:
                    return True  # Exact match

                #  Partial match (more robust)
                if normalized_input_title in normalized_item_title or normalized_item_title in normalized_input_title:
                    return True
        return False  # No match found
    else:
        logging.warning(f"Crossref API request failed with status code: {response.status_code}")
    return False


def search_title(title: str) -> bool:
    """Searches for a title using Crossref, then Scholarly if Crossref fails."""
    if search_title_crossref(title):
        return True
    else:
        return search_title_scholarly(title)


# --- Main Workflow ---

def veri_ex_citing(pdf_path: str) -> Tuple[int, int, int, List[str]]:
    # 1. Extract text from PDF and find bibliography
    full_text = extract_text_from_pdf(pdf_path)
    bib_text = extract_bibliography_section(full_text)
    # print("Extracted Bibliography Section:\n", bib_text, "\n")

    # 2. Split into individual references
    references = split_references(bib_text)
    # print(f"Found {len(references)} references.")

    # 3. Verify each reference
    count_verified, count_warning, count_skipped = 0, 0, 0
    list_warning = []

    for idx, ref in enumerate(references):
        if ref.type == "website":
            count_skipped += 1
            continue

        # print(f"Reference {idx + 1}: {ref}")
        result = search_title(ref.title)

        if result:
            # print("Reference verified.")
            count_verified += 1
        else:
            # print("WARNING: This reference may be fabricated or AI-generated.")
            count_warning += 1
            list_warning.append(ref.input_bibliography)
    return count_verified, count_warning, count_skipped, list_warning


def process_folder(folder_path: str, output_filename: str = None) -> None:
    pdf_files = [f for f in os.listdir(folder_path) if f.endswith('.pdf')]
    pdf_files.sort()
    print(f"Found {len(pdf_files)} PDF files in the folder.")

    results = []
    for pdf_file in pdf_files:
        pdf_path = os.path.join(folder_path, pdf_file)
        print(f"Checking file: {pdf_file}")
        count_verified, count_warning, count_skipped, list_warning = veri_ex_citing(pdf_path)
        print(f"{count_verified} references verified, {count_warning} warnings.")
        if count_warning > 0:
            print("WARNING LIST:")
            for warning in list_warning:
                print(warning)
        print("--------------------------------------------------")
        results.append({"File": pdf_file, "Found References": count_verified + count_warning + count_skipped,
                        "Skipped website": count_skipped, "Verified": count_verified,
                        "Warnings": count_warning, "Warning List": list_warning})
        pd.DataFrame(results).to_csv('VeriCite results.csv', index=False)
    print("Results saved to VeriCite results.csv")



if __name__ == "__main__":
    ''' Example usage #1: check a single PDF file '''
    #     pdf_path = "example.pdf"
    #     count_verified, count_warning, list_warning = VeciCite(pdf_path)
    #     print(f"\n{count_verified} references verified, {count_warning} warnings.")

    ''' Example usage #2: check all PDF files in a folder '''
    # Please replace the folder path to your directory containing the PDF files.
    folder_path = "/Users/yk/Library/CloudStorage/OneDrive-Personal/2024-PhD/CASA0007/Assessment/Yikang_Copies"
    process_folder(folder_path)
