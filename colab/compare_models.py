# %% [markdown]
# # Model karşılaştırma (The Long Ago)
# Aynı 4 sahneyi iki modelle çizer ve yan yana gösterir: **Z-Image-Turbo** (şu an kullanılan, hızlı) ve
# **Qwen-Image** (20B, Apache-2.0, daha büyük). Runtime: **A100**. GitHub anahtarı gerekmez. **Run all** yeterli.

# %% Kurulum
import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "diffusers>=0.43.0", "transformers", "accelerate", "bitsandbytes"])

# %% Sahneler
STYLE = ("Style: painterly historical illustration, soft oil painting, muted warm earth palette, gentle candle or "
         "daylight, cinematic composition, detailed everyday objects, no text, no letters, no watermark, not "
         "photorealistic. Anonymous ordinary people only. wide cinematic landscape composition")
SCENES = [
    "An ancient Egyptian stonecutter in a linen kilt chisels a limestone block in a sunlit workshop at Deir el-Medina, copper tools on a bench",
    "A family of tomb builders shares an evening meal of bread, onions and beer on a flat rooftop, oil lamp glowing, Theban hills at dusk",
    "Close view of a scribe's hands writing on a pottery shard with a reed brush, ink palette beside him, warm lamplight",
    "A medieval miller and his wife carry sacks of flour beside a wooden water wheel at dawn, mist over the river",
]
PROMPTS = [f"{s}. {STYLE}" for s in SCENES]

# %% Çizim
import gc, time, torch
from IPython.display import display
from PIL import Image

def run(name, load, kwargs, size):
    print(f"\n=== {name} yükleniyor ===")
    pipe = load()
    out = []
    for i, p in enumerate(PROMPTS):
        t = time.time()
        img = pipe(prompt=p, width=size[0], height=size[1],
                   generator=torch.Generator("cuda").manual_seed(1000 + i), **kwargs).images[0]
        print(f"{name}: sahne {i + 1} ({time.time() - t:.1f} sn)")
        out.append(img)
    del pipe; gc.collect(); torch.cuda.empty_cache()
    return out

def load_zimage():
    from diffusers import ZImagePipeline
    return ZImagePipeline.from_pretrained("Tongyi-MAI/Z-Image-Turbo", torch_dtype=torch.bfloat16).to("cuda")

def load_qwen():
    from diffusers import PipelineQuantizationConfig, QwenImagePipeline
    vram = torch.cuda.get_device_properties(0).total_memory / 1024**3
    if vram < 70:  # 20B transformer is ~41 GB in bf16: on A100-40GB store it in 8-bit (~21 GB)
        quant = PipelineQuantizationConfig(quant_backend="bitsandbytes_8bit", components_to_quantize=["transformer"])
        pipe = QwenImagePipeline.from_pretrained("Qwen/Qwen-Image", torch_dtype=torch.bfloat16,
                                                 quantization_config=quant)
        pipe.enable_model_cpu_offload()
    else:
        pipe = QwenImagePipeline.from_pretrained("Qwen/Qwen-Image", torch_dtype=torch.bfloat16).to("cuda")
    return pipe

z = run("Z-Image-Turbo", load_zimage, {"num_inference_steps": 9, "guidance_scale": 0.0}, (1920, 1088))
q = run("Qwen-Image", load_qwen, {"num_inference_steps": 40, "true_cfg_scale": 4.0,
        "negative_prompt": "text, letters, watermark, deformed hands, extra fingers, photo, blurry"}, (1664, 928))

# %% Yan yana (solda Z-Image-Turbo, sağda Qwen-Image)
for i, (a, b) in enumerate(zip(z, q), 1):
    a2, b2 = a.resize((960, 544)), b.resize((960, 536))
    canvas = Image.new("RGB", (1930, 544), "white")
    canvas.paste(a2, (0, 0)); canvas.paste(b2, (970, 4))
    print(f"Sahne {i}: SOL = Z-Image-Turbo, SAĞ = Qwen-Image")
    display(canvas)

# %% Bitince GPU'yu kapat (birim harcanmasın)
from google.colab import runtime
runtime.unassign()
