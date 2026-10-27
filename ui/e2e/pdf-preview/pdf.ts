// A minimal PDF writer for the preview suite, so every fixture document is generated from a
// readable description instead of checked in as an opaque binary.
//
// Each page is US Letter with a large filled band in its own colour (what the spec samples from the
// rendered canvas to prove *which* page was drawn) and a "Page N" label. ``cjk`` adds a line set in
// a non-embedded Adobe-GB1 font through the predefined ``UniGB-UCS2-H`` CMap — the shape of PDFs
// many generators emit for Chinese text, and one pdf.js can only decode with its bundled CMaps.
// ``radicals`` adds a "月份" line whose ToUnicode maps 月 to the Kangxi radical ⽉ (U+2F49), as the
// fonts Chrome embeds do: it looks right, and copies as a different character unless unified.
// ``rotate`` sets every page's /Rotate, as scanners and some exporters do for landscape pages.
// ``links`` (needs four pages) puts four labelled link annotations on page 1: a web link, an explicit
// destination on page 3 (XYZ, y = LINK_DEST_Y), a named destination resolving to page 4 (Fit), and a
// ``javascript:`` link that must never become clickable.

export type Rgb = readonly [number, number, number];

export const PAGE_WIDTH = 612;
export const PAGE_HEIGHT = 792;
// The coloured band, in PDF user space (origin bottom-left).
export const BAND = { x: 56, y: 240, width: 500, height: 320 } as const;
export const LINK_URL = 'https://example.com/pdf-link';
export const LINK_DEST_Y = 400;
// Link areas on page 1, in PDF user space, each over its label.
export const LINKS = {
  web: { label: 'example.com', rect: [70, 172, 212, 204] },
  page3: { label: 'To page 3', rect: [248, 172, 362, 204] },
  page4: { label: 'To page 4', rect: [398, 172, 512, 204] },
  script: { label: 'Script', rect: [70, 112, 146, 144] },
} as const;

export function pageColor(index: number): Rgb {
  const palette: Rgb[] = [
    [220, 40, 40],
    [40, 170, 60],
    [40, 80, 220],
    [230, 160, 20],
    [150, 50, 200],
    [20, 170, 190],
  ];
  return palette[index % palette.length];
}

const unit = (v: number) => (v / 255).toFixed(3);

export function buildPdf({
  pages,
  cjk = false,
  radicals = false,
  rotate = 0,
  links = false,
}: {
  pages: number;
  cjk?: boolean;
  radicals?: boolean;
  rotate?: 0 | 90 | 180 | 270;
  links?: boolean;
}): Buffer {
  const objects: string[] = [];
  const add = (body: string) => {
    objects.push(body);
    return objects.length;
  };

  const catalog = add('');
  const pagesNode = add('');
  const helvetica = add('<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>');
  let song = 0;
  if (cjk) {
    const descriptor = add(
      '<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 /FontBBox [-25 -254 1000 880] /ItalicAngle 0 /Ascent 880 /Descent -120 /CapHeight 880 /StemV 93 >>',
    );
    const cidFont = add(
      `<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> /FontDescriptor ${descriptor} 0 R /DW 1000 >>`,
    );
    let toUnicode = '';
    if (radicals) {
      const cmap = [
        '/CIDInit /ProcSet findresource begin 12 dict begin begincmap',
        '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def',
        '/CMapName /Adobe-Identity-UCS def /CMapType 2 def',
        '1 begincodespacerange <0000> <FFFF> endcodespacerange',
        '6 beginbfchar <4E2D> <4E2D> <6587> <6587> <9884> <9884> <89C8> <89C8> <6708> <2F49> <4EFD> <4EFD> endbfchar',
        'endcmap CMapName currentdict /CMap defineresource pop end end',
      ].join('\n');
      toUnicode = ` /ToUnicode ${add(`<< /Length ${Buffer.byteLength(cmap)} >>\nstream\n${cmap}\nendstream`)} 0 R`;
    }
    song = add(`<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light-UniGB-UCS2-H /Encoding /UniGB-UCS2-H /DescendantFonts [${cidFont} 0 R]${toUnicode} >>`);
  }

  const kids: number[] = [];
  for (let i = 0; i < pages; i += 1) {
    const [r, g, b] = pageColor(i);
    const ops = [
      `${unit(r)} ${unit(g)} ${unit(b)} rg ${BAND.x} ${BAND.y} ${BAND.width} ${BAND.height} re f`,
      `0 0 0 rg BT /F1 48 Tf 72 680 Td (Page ${i + 1}) Tj ET`,
    ];
    // "中文预览" as UCS-2 code units.
    if (cjk) ops.push('0 0 0 rg BT /F2 40 Tf 72 620 Td <4E2D6587988489C8> Tj ET');
    // "月份", in the band's upper edge.
    if (radicals) ops.push('0 0 0 rg BT /F2 32 Tf 72 575 Td <67084EFD> Tj ET');
    if (links && i === 0) {
      for (const { label, rect } of Object.values(LINKS)) ops.push(`0 0 0 rg BT /F1 24 Tf ${rect[0] + 2} ${rect[1] + 8} Td (${label}) Tj ET`);
    }
    const stream = ops.join('\n');
    const content = add(`<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`);
    const fonts = `/F1 ${helvetica} 0 R${cjk ? ` /F2 ${song} 0 R` : ''}`;
    kids.push(
      add(`<< /Type /Page /Parent ${pagesNode} 0 R /MediaBox [0 0 ${PAGE_WIDTH} ${PAGE_HEIGHT}]${rotate ? ` /Rotate ${rotate}` : ''} /Contents ${content} 0 R /Resources << /Font << ${fonts} >> >> >>`),
    );
  }
  let names = '';
  if (links) {
    const box = (rect: readonly number[]) => `/Rect [${rect.join(' ')}] /Border [0 0 0]`;
    const annots = [
      add(`<< /Type /Annot /Subtype /Link ${box(LINKS.web.rect)} /A << /S /URI /URI (${LINK_URL}) >> >>`),
      add(`<< /Type /Annot /Subtype /Link ${box(LINKS.page3.rect)} /Dest [${kids[2]} 0 R /XYZ 0 ${LINK_DEST_Y} null] >>`),
      add(`<< /Type /Annot /Subtype /Link ${box(LINKS.page4.rect)} /Dest (page-four) >>`),
      add(`<< /Type /Annot /Subtype /Link ${box(LINKS.script.rect)} /A << /S /URI /URI (javascript:alert\\(1\\)) >> >>`),
    ];
    objects[kids[0] - 1] = objects[kids[0] - 1].replace(' /Contents', ` /Annots [${annots.map((a) => `${a} 0 R`).join(' ')}] /Contents`);
    names = ` /Names << /Dests << /Names [(page-four) [${kids[3]} 0 R /Fit]] >> >>`;
  }
  objects[catalog - 1] = `<< /Type /Catalog /Pages ${pagesNode} 0 R${names} >>`;
  objects[pagesNode - 1] = `<< /Type /Pages /Kids [${kids.map((k) => `${k} 0 R`).join(' ')}] /Count ${kids.length} >>`;

  let out = '%PDF-1.4\n';
  const offsets: number[] = [];
  objects.forEach((body, i) => {
    offsets.push(Buffer.byteLength(out));
    out += `${i + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = Buffer.byteLength(out);
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets) out += `${String(offset).padStart(10, '0')} 00000 n \n`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root ${catalog} 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}
