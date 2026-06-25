import unittest
from extract_html import get_heading_from_html, get_first_paragraph_from_html, get_urls_from_html, get_images_from_html, extract_page_data

class TestExtractHTML(unittest.TestCase):
    def test_get_heading_from_html(self):
        input_body = '<html><body><h1>Test Title</h1></body></html>'
        actual = get_heading_from_html(input_body)
        expected = "Test Title"
        self.assertEqual(actual, expected)

    def test_get_first_paragraph_from_html_1(self):
        input_body = '''<html><body>
        <p>Outside paragraph.</p>
        <main>
            <p>Main paragraph.</p>
        </main>
        </body></html>'''
        actual = get_first_paragraph_from_html(input_body)
        expected = "Main paragraph."
        self.assertEqual(actual, expected)

    def test_get_first_paragraph_from_html_2(self):
        input_body = '''<html><body>
        <p>Outside paragraph.</p>
        </body></html>'''
        actual = get_first_paragraph_from_html(input_body)
        expected = "Outside paragraph."
        self.assertEqual(actual, expected)

    def test_get_urls_from_html_1(self):
            input_url = "https://crawler-test.com"
            input_body = '<html><body><a href="https://crawler-test.com"><span>Boot.dev</span></a></body></html>'
            actual = get_urls_from_html(input_body, input_url)
            expected = ["https://crawler-test.com"]
            self.assertEqual(actual, expected)

    def test_get_urls_from_html_2(self):
            input_url = "https://crawler-test.com"
            input_body = '<html><body></body></html>'
            actual = get_urls_from_html(input_body, input_url)
            expected = "No Link Found"
            self.assertEqual(actual, expected)

    def test_get_urls_from_html_3(self):
            input_url = "https://crawler-test.com"
            input_body = '<html><body><a href="/home/contact_us"><span>Boot.dev</span></a></body></html>'
            actual = get_urls_from_html(input_body, input_url)
            expected = ["https://crawler-test.com/home/contact_us"]
            self.assertEqual(actual, expected)

    def test_get_images_from_html_1(self):
        input_url = "https://crawler-test.com"
        input_body = '<html><body><img src="/logo.png" alt="Logo"></body></html>'
        actual = get_images_from_html(input_body, input_url)
        expected = ["https://crawler-test.com/logo.png"]
        self.assertEqual(actual, expected)

    def test_get_images_from_html_2(self):  
        input_url = "https://crawler-test.com"
        input_body = '<html><body></body></html>'
        actual = get_images_from_html(input_body, input_url)
        expected = "No Image Found"
        self.assertEqual(actual, expected)

    def test_get_images_from_html_3(self):
        input_url = "https://crawler-test.com"
        input_body = '<html><body><img src="https://crawler-test.com/logo.png" alt="Logo"></body></html>'
        actual = get_images_from_html(input_body, input_url)
        expected = ["https://crawler-test.com/logo.png"]
        self.assertEqual(actual, expected)

    def test_extract_page_data_1(self):
        input_url = "https://crawler-test.com"
        input_body = '''<html><body>
            <h1>Test Title</h1>
            <p>This is the first paragraph.</p>
            <a href="/link1">Link 1</a>
            <img src="/image1.jpg" alt="Image 1">
        </body></html>'''
        actual = extract_page_data(input_body, input_url)
        expected = {
            "url": "https://crawler-test.com",
            "heading": "Test Title",
            "first_paragraph": "This is the first paragraph.",
            "outgoing_links": ["https://crawler-test.com/link1"],
            "image_urls": ["https://crawler-test.com/image1.jpg"]
        }
        
        self.assertEqual(actual, expected)

    def test_extract_page_data_2(self):
        input_url = "https://crawler-test.com"
        input_body = '''<html><body>
            <p>This is the first paragraph.</p>
            <a href="/link1">Link 1</a>
            <img src="/image1.jpg" alt="Image 1">
        </body></html>'''
        actual = extract_page_data(input_body, input_url)
        expected = {
            "url": "https://crawler-test.com",
            "heading": "",
            "first_paragraph": "This is the first paragraph.",
            "outgoing_links": ["https://crawler-test.com/link1"],
            "image_urls": ["https://crawler-test.com/image1.jpg"]
        }
        
        self.assertEqual(actual, expected)

    def test_extract_page_data_3(self):
        input_url = "https://crawler-test.com"
        input_body = '''<html><body>
            <h1>Test Title</h1>
            <p>This is the first paragraph.</p>
        </body></html>'''
        actual = extract_page_data(input_body, input_url)
        expected = {
            "url": "https://crawler-test.com",
            "heading": "Test Title",
            "first_paragraph": "This is the first paragraph.",
            "outgoing_links": 'No Link Found',
            "image_urls": 'No Image Found'
        }
        
        self.assertEqual(actual, expected)
        
if __name__ == "__main__" :
    unittest.main()
