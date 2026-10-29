// CJK radicals → the unified ideograph each one is equivalent to, for text extracted from PDFs.
//
// Fonts that Chrome and other generators embed often map common characters (月, 自, 行, 页 …) to the
// Kangxi Radicals and CJK Radicals Supplement code points that look the same. The text reads
// correctly on screen but copies as different characters, so pasted text no longer matches a search
// or the original. pdf.js's own normalization leaves both blocks alone, and NFKC would also turn
// full-width punctuation into ASCII, so these blocks are mapped on their own.
//
// Generated from Unicode's EquivalentUnifiedIdeograph-18.0.0.txt (U+2E80–U+2FDF entries), as
// consecutive (radical, ideograph) pairs. Regenerate rather than edit by hand.
const PAIRS =
  '⺁厂⺂乛⺃乚⺄乙⺅亻⺆冂⺇𠘨⺈刀⺉刂⺊卜⺋㔾⺌小⺍小⺎兀⺏尣⺐尢⺑𡯂⺒巳⺓幺⺔彑⺕𫜹⺖忄⺗心⺘扌⺙攵⺛旡⺜日⺝月⺞歺⺟母⺠民⺡氵⺢氺⺣灬⺤爫⺥爫⺦丬⺧牛⺨犭⺩王' +
  '⺪𤴔⺫目⺬示⺭礻⺮𥫗⺯糹⺰纟⺱罓⺲罒⺳㓁⺴冗⺵𦉫⺶羊⺷𦍌⺸𦍋⺹耂⺺肀⺻聿⺼肉⺽𦥑⺾艹⺿艹⻀艹⻁虎⻂衤⻃覀⻄西⻅见⻆角⻇𧢲⻈讠⻉贝⻊𧾷⻋车⻌辶⻍辶⻎辶⻏邑⻐钅⻑長' +
  '⻒镸⻓长⻔门⻕𨸏⻖阝⻗雨⻘青⻙韦⻚页⻛风⻜飞⻝食⻞𩙿⻟飠⻠饣⻡𩠐⻢马⻣骨⻤鬼⻥鱼⻦鸟⻧卤⻨麦⻩黄⻪黾⻫斉⻬齐⻭歯⻮齿⻯竜⻰龙⻱龜⻲亀⻳龟⼀一⼁丨⼂丶⼃丿⼄乙⼅亅' +
  '⼆二⼇亠⼈人⼉儿⼊入⼋八⼌冂⼍冖⼎冫⼏几⼐凵⼑刀⼒力⼓勹⼔匕⼕匚⼖匸⼗十⼘卜⼙卩⼚厂⼛厶⼜又⼝口⼞囗⼟土⼠士⼡夂⼢夊⼣夕⼤大⼥女⼦子⼧宀⼨寸⼩小⼪尢⼫尸⼬屮⼭山' +
  '⼮巛⼯工⼰己⼱巾⼲干⼳幺⼴广⼵廴⼶廾⼷弋⼸弓⼹彐⼺彡⼻彳⼼心⼽戈⼾戶⼿手⽀支⽁攴⽂文⽃斗⽄斤⽅方⽆无⽇日⽈曰⽉月⽊木⽋欠⽌止⽍歹⽎殳⽏毋⽐比⽑毛⽒氏⽓气⽔水⽕火' +
  '⽖爪⽗父⽘爻⽙爿⽚片⽛牙⽜牛⽝犬⽞玄⽟玉⽠瓜⽡瓦⽢甘⽣生⽤用⽥田⽦疋⽧疒⽨癶⽩白⽪皮⽫皿⽬目⽭矛⽮矢⽯石⽰示⽱禸⽲禾⽳穴⽴立⽵竹⽶米⽷糸⽸缶⽹网⽺羊⽻羽⽼老⽽而' +
  '⽾耒⽿耳⾀聿⾁肉⾂臣⾃自⾄至⾅臼⾆舌⾇舛⾈舟⾉艮⾊色⾋艸⾌虍⾍虫⾎血⾏行⾐衣⾑襾⾒見⾓角⾔言⾕谷⾖豆⾗豕⾘豸⾙貝⾚赤⾛走⾜足⾝身⾞車⾟辛⾠辰⾡辵⾢邑⾣酉⾤釆⾥里' +
  '⾦金⾧長⾨門⾩阜⾪隶⾫隹⾬雨⾭靑⾮非⾯面⾰革⾱韋⾲韭⾳音⾴頁⾵風⾶飛⾷食⾸首⾹香⾺馬⾻骨⾼高⾽髟⾾鬥⾿鬯⿀鬲⿁鬼⿂魚⿃鳥⿄鹵⿅鹿⿆麥⿇麻⿈黃⿉黍⿊黑⿋黹⿌黽⿍鼎' +
  '⿎鼓⿏鼠⿐鼻⿑齊⿒齒⿓龍⿔龜⿕龠';

// By code point: a few ideographs are outside the BMP (two UTF-16 units each).
const CHARS = Array.from(PAIRS);
const EQUIVALENT = new Map<string, string>();
for (let i = 0; i < CHARS.length; i += 2) EQUIVALENT.set(CHARS[i], CHARS[i + 1]);

const RADICAL = /[\u2E80-\u2FDF]/g;

/** ``text`` with every CJK radical that has an equivalent unified ideograph replaced by it. */
export function unifyCjkRadicals(text: string): string {
  return text.replace(RADICAL, (ch) => EQUIVALENT.get(ch) ?? ch);
}
