# 词表：Blender 术语英中对照（建模 / 合成 / 渲染）

翻译技术字幕时**优先使用下表译法**（覆盖临时译法）；词表未收录的术语按 SKILL.md 规则处理：首次出现用「中文（English）」格式，之后只用中文。

标注「保留」的条目一律保留英文原样（多为界面名称、节点名、混合模式），不音译、不翻译。

## 界面与通用

| English | 中文 |
|---|---|
| Viewport | 视图（不要译成「视口」） |
| Outliner | 大纲视图 |
| Object Mode | 物体模式 |
| Edit Mode | 编辑模式 |
| Vertex Select / Edge Select / Face Select | 顶点 / 边 / 面 选择模式 |
| Add Mesh | 添加网格 |
| UV Sphere | UV 球体 |
| Last Operation (panel) | 最近操作面板 |
| Wrench icon | 扳手图标 |
| Render | 渲染 |
| Frame Selected | 框显所选 |
| Wireframe | 线框（显示模式） |
| Object Data Properties | 物体数据属性 |
| Selection Tool | 选择工具 |
| Move Tool | 移动工具 |
| Scale Tool | 缩放工具 |
| Box Select | 框选 |
| In Edit Mode | 在编辑模式中显示 |
| Assign / Select / Deselect | 指定 / 选择 / 取消选择 |

## 建模操作与概念

| English | 中文 |
|---|---|
| Box Modeling | 方块建模 |
| Subdivision Surface / Sub-D modeling | 表面细分 / 表面细分建模（不要译「细分曲面」） |
| Subdivision Surface Modifier | 表面细分修改器 |
| primitive shape | 基本几何体 |
| base mesh | 基础网格 |
| vertices / vert | 顶点 |
| edge loop | 循环边 |
| n-gon | n-gon（多边形面，保留原文） |
| Inset | 内插面 |
| Bridge Edge Loops | 桥接循环边 |
| Loop Cut | 循环切割 |
| Bevel | 倒角 |
| Extrude | 挤出 |
| Extrude Region | 挤出区域 |
| Extrude Along Normals | 沿法线挤出 |
| Split | 分离 |
| Spin | 旋转 |
| Snapping | 吸附 |
| Vertex Snapping | 顶点吸附 |
| Normal | 法线 |
| Modifier | 修改器 |
| Solidify (Modifier) | 实体化（修改器） |
| Vertex Group | 顶点组 |
| Thickness / Offset | 厚度 / 偏移 |
| Segments / Rings | 段数 / 环数 |
| Shade Smooth | 平滑着色 |
| triangle fan | 三角扇 |
| Uniform Scale | 等比缩放 |

## 合成与渲染通道（Compositing & Passes）

| English | 中文 |
|---|---|
| compositing | 合成 |
| compositor | 合成器 |
| pass / render pass | 渲染通道（简称「通道」） |
| multipass | 多通道 |
| beauty pass | beauty 通道（beauty 保留） |
| Combined (pass) | Combined（保留） |
| diffuse | 漫反射（节点名 Diffuse 保留） |
| Diffuse Direct / Indirect / Color | Diffuse Direct / Indirect / Color（保留） |
| glossy | 光泽 / 高光（节点名 Glossy 保留） |
| Glossy Direct / Indirect / Color | Glossy Direct / Indirect / Color（保留） |
| transmission | 透射（节点名 Transmission 保留） |
| emission | 自发光（节点名 Emission 保留） |
| volume / volumetrics | 体积 / 体积雾 |
| environment (pass) | 环境 |
| occlusion | 遮挡 |
| mist pass | 雾通道 |
| Z-depth | Z 深度 |
| light path | 光程 |
| caustics | 焦散 |
| bounce | 反弹（次数） |
| ray tracing | 光线追踪 |
| adaptive sampling | 自适应采样 |
| samples | 采样 |
| denoise | 降噪 |
| clamp | 钳制 |
| render layer | 渲染层 |
| view layer | 视图层 |
| Use for Rendering | Use for Rendering（保留，视图层属性开关） |
| Film Transparent | 透明背景（Film Transparent） |
| image sequence | 图像序列 |
| Import Image Sequence | Import Image Sequence（保留） |
| EXR / OpenEXR | EXR / OpenEXR（保留） |
| MultiLayer (EXR) | 多图层（MultiLayer） |
| 16-bit / 32-bit float | 16 位 / 32 位浮点 |
| bit depth | 位深度 |
| Render Size | 渲染尺寸（Render Size） |
| portal | 门户（Portal） |
| starter kit | starter kit（保留，配套素材包） |
| alpha | alpha（透明度） |
| Set Alpha | Set Alpha（保留） |
| Alpha Over | Alpha Over（保留） |
| Replace / Apply (alpha mode) | Replace / Apply（保留，alpha 模式） |
| Factor | Factor（保留，混合系数） |
| daisy-chain | 串成链（节点串联） |
| spiderweb | 蜘蛛网（形容节点连线复杂） |

## 合成器节点与混合模式

| English | 中文 |
|---|---|
| Mix (node) | Mix（保留） |
| Multiply / Add / Screen | Multiply / Add / Screen（保留，混合模式） |
| Overlay | 叠加（Overlay；界面模式名保留） |
| Exposure (node) | 曝光（Exposure 节点） |
| Transform / Scale (node) | Transform / Scale（节点名，保留） |
| Blur (node) | 模糊（Blur 节点） |
| Mask (node) | 遮罩（Mask 节点） |
| ColorRamp | 颜色渐变（ColorRamp） |
| Hue Saturation Value | Hue Saturation Value（保留） |
| Color Balance | 色彩平衡（Color Balance） |
| Lift / Gamma / Gain | Lift / Gamma / Gain（保留） |
| Hue Correct | Hue Correct（保留） |
| Separate RGBA / Combine RGB | 分离 RGBA / 合并 RGB |
| Backdrop | 背板 |
| Node Wrangler | Node Wrangler（保留） |
| Viewer (node) | 查看器（Viewer 节点） |
| Composite (node) | 合成输出（Composite 节点） |
| Use Nodes | Use Nodes（保留） |
| shader | 着色器 |
| base color | 基础色（Base Color） |
| material preview | 材质预览 |
| color grade | 调色（color grade） |
| monochromatic color grade | 单色调色 |
| complementary color | 互补色 |

## 视觉效果与美化（Beautification）

| English | 中文 |
|---|---|
| beautification | 美化 |
| integration | 整合 |
| matte painting | 数字绘景 |
| depth | 纵深 / 深度 |
| light wrap | 光环绕 |
| vignette | 暗角 |
| god rays | 丁达尔光 |
| bloom | 泛光 |
| light leak | 漏光 |
| film grain | 胶片颗粒 |
| dust elements | 灰尘元素 |
| chromatic aberration | 色差 |
| dispersion | 色散 |
| cryptomatte | cryptomatte（加密遮罩） |
| crypto object / crypto material | Crypto Object / Crypto Material（保留，Cryptomatte 选择模式） |
| matte | 遮罩（matte） |
| specular | 高光 |
| reflection / refraction | 反射 / 折射 |
| color space | 色彩空间 |
| post-processing | 后处理 |
| Previz (previsualization) | 预览（Previz） |
| HDRI | HDRI（保留） |
| grain cleanup | 颗粒清理 / 噪点清理 |

## ASR 常见误听（自动字幕识别错误，翻译时按右列理解）

| ASR 误记 | 实际词 |
|---|---|
| Evie / EV | Eevee |
| GBU compute | GPU compute |
| CBU | CPU |
| caught six | caustics |
| known network | node network |
| services（Filters 处） | surfaces |
| retracing | ray tracing |
| missive | emission |
| important image sequence | import image sequence |
| subdivision service | subdivision surface |
| blazoning fast | blazing fast |
| verts | vertices（口语，正常译「顶点」） |
| portfolio（"same exact portfolio"） | 应为结构/组成，译「结构完全相同」 |
| gas mask rough | 材质名，保留原文 |
