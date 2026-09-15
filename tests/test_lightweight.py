import tempfile
import unittest
from pathlib import Path
from langgraph.checkpoint.memory import InMemorySaver
from scimetrics.document import find_images, build_content
from scimetrics.pipeline import Settings, build_graph
from scimetrics.models import ImageDecision, Discovery, Extraction


class Client:
    def __init__(self):
        self.calls = []

    def call(self, stage, prompt, parts, schema):
        self.calls.append((stage, parts))
        if schema is ImageDecision:
            return ImageDecision(label='decorative' if 'logo' in parts[1]['image_url']['url'] else 'scientific', reason='fixture')
        if schema is Discovery:
            return Discovery(indicators=[])
        return Extraction(indicators=[], formulas=[], reported_limitations=[], inferred_limitations=[],
                          paper_relations=[], indicator_relations=[])


class LightweightTests(unittest.TestCase):
    def test_positions_and_filtering(self):
        raw = '中文前![a](https://example.com/a.png)中![logo](https://example.com/logo.png)后![a](https://example.com/a.png)末'
        images = find_images(raw)
        self.assertEqual(len(images), 2)
        self.assertEqual(len(images[0]['occurrences']), 2)
        for image in images:
            image['classification'] = {'label': 'decorative' if 'logo' in image['url'] else 'scientific'}
            for o in image['occurrences']:
                self.assertTrue(raw[o['start']:o['end']].startswith('!['))
        parts = build_content(raw, images)
        self.assertEqual(sum(p['type'] == 'image_url' for p in parts), 2)
        text = ''.join(p['text'] for p in parts if p['type'] == 'text')
        self.assertNotIn('![', text)
        self.assertNotIn('logo.png', text)
        self.assertIn('中文前', parts[0]['text'])
        self.assertEqual(parts[2]['type'], 'image_url')
        self.assertEqual(raw.count('!['), 3)

    def test_reference_html_code_escape(self):
        raw = ('![ref][r]\n\n<img src="https://example.com/b.png" alt="B">\n\n'
               '`![inline](https://example.com/fake.png)`\n\n'
               '```md\n![fence](https://example.com/fake.png)\n```\n\n'
               '    ![indent](https://example.com/fake.png)\n\n'
               '\\![escape](https://example.com/fake.png)\n\n'
               '[r]: https://example.com/a.png\n')
        images = find_images(raw)
        self.assertEqual([i['url'] for i in images], ['https://example.com/a.png', 'https://example.com/b.png'])
        self.assertEqual(raw[images[0]['occurrences'][0]['start']:images[0]['occurrences'][0]['end']], '![ref][r]')

    def test_limits_and_local_url(self):
        with self.assertRaises(ValueError):
            find_images('![local](local.png)')
        with self.assertRaises(ValueError):
            build_content('long', [], max_chars=1)
        self.assertEqual(build_content('plain text', [])[0]['text'], '[B000001]\nplain text')

    def test_nodes_checkpoint_and_downstream(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)/'paper.md'
            raw = '前![a](https://example.com/a.png)后'
            source.write_text(raw)
            client = Client()
            checkpointer = InMemorySaver()
            graph = build_graph(Settings(source, Path(d)/'out'), client, checkpointer=checkpointer,
                                interrupt_after=['prepare_document', 'classify_images'])
            cfg = {'configurable': {'thread_id': 'test'}}
            graph.invoke({}, cfg)
            before = graph.get_state(cfg).values
            self.assertEqual(set(before), {'raw_content', 'images'})
            self.assertEqual(before['raw_content'], raw)
            self.assertIsNone(before['images'][0]['classification'])
            graph.invoke(None, cfg)
            after = graph.get_state(cfg).values
            self.assertEqual(after['images'][0]['classification']['label'], 'scientific')
            self.assertIsNone(before['images'][0]['classification'])
            graph.invoke(None, cfg)
            self.assertEqual([s for s, _ in client.calls], ['classify_F0001', 'discover', 'extract'])
            self.assertTrue((Path(d)/'out'/'result.json').exists())
            self.assertNotIn('document', graph.get_state(cfg).values)


if __name__ == '__main__':
    unittest.main()
