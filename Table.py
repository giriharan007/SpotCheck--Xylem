from transformers import AutoImageProcessor, TableTransformerForObjectDetection

processor = AutoImageProcessor.from_pretrained(
    "microsoft/table-transformer-detection"
)

model = TableTransformerForObjectDetection.from_pretrained(
    "microsoft/table-transformer-detection"
)

print("Model downloaded successfully")