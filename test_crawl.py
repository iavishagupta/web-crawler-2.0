import unittest
from crawl import normalize_url

class TestCrawl(unittest.TestCase) :
    def test_normalize_url(self):
        input_url = "https://www.boot.dev/blog/path"
        actual = normalize_url(input_url)
        expected = "www.boot.dev/blog/path"
        self.assertEqual(actual, expected)

if __name__ == "__main__":
    unittest.main()
