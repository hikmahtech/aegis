"""Docker e2e tests for content extraction.

NOTE: PDF extraction, image OCR, and HTML parsing have been removed from the
AEGIS worker as part of the knowledge-first integration. These capabilities
are now handled by knowledge-service.

The original tests for _extract_pdf_text, _ocr_image, and _ocr_pdf have been
deleted. See test_content_simplified.py for the new offload-based tests.
"""
