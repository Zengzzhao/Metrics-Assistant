import { createMarkdownRenderer } from 'vue-mdr'
import remarkMath from 'remark-math'
import rehypeKatex from 'rehype-katex'
import type { Plugin } from 'unified'
import 'katex/dist/katex.min.css'

// 该版本会先将表格 AST 放入自定义节点，需同时转换表格内的公式。
const mathWithTables: typeof rehypeKatex = (options) => {
  const transform = rehypeKatex(options)
  return (tree, file) => {
    transform(tree, file)
    type Node = typeof tree | (typeof tree.children)[number]
    function visit(node: Node) {
      if (node.type === 'element' && node.tagName === 'TableRenderer') {
        const ast = node.properties.ast as unknown as (typeof tree.children)[number]
        transform({ type: 'root', children: [ast] }, file)
      }
      if ('children' in node) node.children.forEach(visit)
    }
    visit(tree)
  }
}
// vue-mdr 0.1.3：先清理用户内容，再将数学节点转换为 KaTeX。
export const Renderer = createMarkdownRenderer({
  remarkPlugins: [remarkMath],
  rehypePlugins: [mathWithTables as unknown as Plugin],
  remarkRehypeOptions: { allowDangerousHtml: false },
  rehypeSanitizeSchema: {
    attributes: { code: [['className', /^language-./, 'math-inline', 'math-display']] },
  },
})
