from transformers import RoFormerForCausalLM, RoFormerModel, RoFormerConfig, GenerationConfig
from transformers.generation.logits_process import LogitsProcessor, LogitsProcessorList
import torch
from torch import nn
from text.symbols import *
from cluster import get_cluster_model

def get_model(mode = "phone", semantic_kmeans_num = 10000, codebook_path = "pretrain/semantic_codebook.pt", n_spk = 1, **kwargs):
    encoder_config = RoFormerConfig(
            hidden_size=kwargs["model"]["encoder"]["hidden_size"],
            num_attention_heads=kwargs["model"]["encoder"]["num_attention_heads"],
            num_hidden_layers=kwargs["model"]["encoder"]["num_hidden_layers"],
            intermediate_size=kwargs["model"]["encoder"]["intermediate_size"],
            hidden_act=kwargs["model"]["encoder"]["hidden_act"],
            hidden_dropout_prob=kwargs["model"]["encoder"]["hidden_dropout_prob"],
            attention_probs_dropout_prob=kwargs["model"]["encoder"]["attention_probs_dropout_prob"],
            initializer_range=kwargs["model"]["encoder"]["initializer_range"],
            layer_norm_eps=float(kwargs["model"]["encoder"]["layer_norm_eps"]),
            max_position_embeddings = kwargs["model"]["encoder"]["max_position_embeddings"]
        )

    decoder_config = RoFormerConfig(
            hidden_size=kwargs["model"]["decoder"]["hidden_size"],
            num_attention_heads=kwargs["model"]["decoder"]["num_attention_heads"],
            num_hidden_layers=kwargs["model"]["decoder"]["num_hidden_layers"],
            intermediate_size=kwargs["model"]["decoder"]["intermediate_size"],
            hidden_act=kwargs["model"]["decoder"]["hidden_act"],
            hidden_dropout_prob=kwargs["model"]["decoder"]["hidden_dropout_prob"],
            attention_probs_dropout_prob=kwargs["model"]["decoder"]["attention_probs_dropout_prob"],
            initializer_range=kwargs["model"]["decoder"]["initializer_range"],
            layer_norm_eps=float(kwargs["model"]["decoder"]["layer_norm_eps"]),
            max_position_embeddings = kwargs["model"]["decoder"]["max_position_embeddings"]
    )
    
    model = Roformer(
        encoder_config = encoder_config,
        decoder_config = decoder_config,
        mode = mode,
        semantic_kmeans_num = semantic_kmeans_num,
        codebook_path = codebook_path,
        n_spk = n_spk
    )

    return model
    
class EndGateLogitsProcessor(LogitsProcessor):
    def __init__(self, end_gate_threshold: float, eos_token_id: int):
        
        self.end_gate_threshold = end_gate_threshold
        self.eos_token_id = eos_token_id

    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> torch.FloatTensor:
        gate = torch.softmax(scores,dim=-1)[:, self.eos_token_id] > self.end_gate_threshold
        scores[gate] = float("inf")
        return scores

class Roformer(nn.Module):
    def __init__(
        self,
        encoder_config: RoFormerConfig,
        decoder_config: RoFormerConfig,
        mode = "phone",
        semantic_kmeans_num = 10000,
        codebook_path = "pretrain/semantic_codebook.pt",
        n_spk = 1,
        **kwargs
        ):
        super().__init__()
        self.mode = mode
        self.n_spk = n_spk
        if "phone" in self.mode:
            token_size = len(symbols)
            self.BOS = token_size
            self.EOS = token_size + 1
            self.PAD = token_size + 2
            self.num_tones = num_tones
            token_size += 3

        encoder_config.vocab_size = token_size
        encoder_config.type_vocab_size = self.num_tones + 1
        encoder_config.pad_token_id = self.PAD
        encoder_config.bos_token_id = self.BOS
        encoder_config.eos_token_id = self.EOS
        self.text_encoder = RoFormerModel(encoder_config)
        
        decoder_config.bos_token_id = semantic_kmeans_num
        decoder_config.eos_token_id = semantic_kmeans_num + 1
        decoder_config.pad_token_id = semantic_kmeans_num + 2
        decoder_config.vocab_size = semantic_kmeans_num + 3

        self.semantic_bos_token_id = semantic_kmeans_num
        self.semantic_eos_token_id = semantic_kmeans_num + 1
        self.semantic_pad_token_id = semantic_kmeans_num + 2

        decoder_config.type_vocab_size = 1
        decoder_config.is_decoder = True
        decoder_config.add_cross_attention = True
        self.semantic_decoder = RoFormerForCausalLM(decoder_config)
        self.semantic_decoder.prepare_inputs_for_generation = self.prepare_inputs_for_generation
        
        self.quantizer = get_cluster_model(codebook_path)

        if self.semantic_decoder.roformer.embeddings.word_embeddings.weight.data.shape[1] == self.quantizer.cluster_centers_.shape[1]:
            self.semantic_decoder.roformer.embeddings.word_embeddings.weight.data[:semantic_kmeans_num] = torch.from_numpy(self.quantizer.cluster_centers_.copy())

        if n_spk > 1 and n_spk is not None:
            self.spk_emb = nn.Embedding(n_spk + 1, encoder_config.hidden_size)
        else:
            self.spk_emb = None

    def forward(
        self,
        phone,
        tone,
        semantic,
        attention_mask=None,
        encoder_attention_mask=None,
        labels=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        spk_id=None,
        **kwargs
    ):
        if self.spk_emb is not None and spk_id is not None:
            spk_emb = self.spk_emb(spk_id)
        else:
            spk_emb = 0

        phone_tone_emb = self.text_encoder.embeddings(phone,tone) + spk_emb
        
        encoder_hidden_states = self.text_encoder(
            inputs_embeds = phone_tone_emb,
            attention_mask = encoder_attention_mask,
            use_cache = use_cache
        ).last_hidden_state
        
        outputs = self.semantic_decoder(
            semantic,
            encoder_hidden_states = encoder_hidden_states,
            encoder_attention_mask = encoder_attention_mask,
            attention_mask = attention_mask,
            labels = labels,
            output_attentions = output_attentions,
            output_hidden_states = output_hidden_states,
            return_dict = return_dict,
            use_cache = use_cache
        )
        return outputs
    
    @torch.no_grad()
    def generate(self,
                 phone,
                 tone,
                 attention_mask=None,
                 use_cache=None,
                 max_length=1024,
                 do_sample=True,
                 temperature=1.0,
                 top_k=5,
                 top_p=0.8,
                 repetition_penalty=1.2,
                 num_beams=1,
                 no_repeat_ngram_size = 0,
                 early_stopping = True,
                 spk_id = None,
                 end_gate_threshold = None,
                 **kwargs
                 ):
        
        logits_processor = LogitsProcessorList()
        if end_gate_threshold is not None:
            logits_processor.append(EndGateLogitsProcessor(end_gate_threshold = end_gate_threshold, eos_token_id = self.semantic_eos_token_id))

        if len(logits_processor) == 0:
            logits_processor = None

        if self.spk_emb is not None and spk_id is not None:
            spk_emb = self.spk_emb(spk_id)
        else:
            spk_emb = 0
        
        phone_tone_emb = self.text_encoder.embeddings(phone,tone) + spk_emb
        
        encoder_hidden_states = self.text_encoder(
            inputs_embeds = phone_tone_emb,
            attention_mask = attention_mask,
            use_cache = use_cache
        )[0]

        if num_beams == 1:
            early_stopping = False

        generation_config = GenerationConfig(
            max_length=max_length,
            do_sample=do_sample,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            repetition_penalty=repetition_penalty,
            num_beams=num_beams,
            no_repeat_ngram_size = no_repeat_ngram_size,
            early_stopping = early_stopping,
            bos_token_id = self.semantic_bos_token_id,
            eos_token_id = self.semantic_eos_token_id,
            pad_token_id = self.semantic_pad_token_id
        )

        outputs = self.semantic_decoder.generate(
            encoder_hidden_states = encoder_hidden_states,
            encoder_attention_mask = attention_mask,
            attention_mask = None,
            use_cache = use_cache,
            generation_config=generation_config,
            logits_processor = logits_processor
        )

        return outputs

    def prepare_inputs_for_generation(self, input_ids, past_key_values=None, attention_mask=None, **model_kwargs):
        input_shape = input_ids.shape

        if attention_mask is None:
            attention_mask = input_ids.new_ones(input_shape)

        if past_key_values is not None:
            input_ids = input_ids[:, -1:]

        return {"input_ids": input_ids, "attention_mask": attention_mask, "past_key_values": past_key_values, **model_kwargs}